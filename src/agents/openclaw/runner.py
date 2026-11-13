from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from dotenv import load_dotenv

from src.agents.base import AgentExecution, AgentTaskSpec, BaseAgent
from src.utils.grading import extract_usage_from_jsonl
from src.utils.trace_artifacts import locally_tokenized_assistant_output
from src.utils.openclaw_runtime import available_gateway_port
from src.utils.docker_utils import (
    inject_lobster_workspace,
    inject_openclaw_models,
    run_background,
    run_warmup,
    setup_skills,
    setup_workspace,
    start_container,
    stop_background_process,
)

load_dotenv()

logger = logging.getLogger(__name__)


class OpenClawAgent(BaseAgent):
    def __init__(
        self,
        gateway_port: int,
        openrouter_api_key: str = "",
        openrouter_base_url: str = "https://openrouter.ai/api/v1",
        image_model: str | None = None,
    ) -> None:
        self.gateway_port = gateway_port
        self.openrouter_api_key = openrouter_api_key
        self.openrouter_base_url = openrouter_base_url
        self.image_model = image_model if image_model is not None else os.environ.get("OPENCLAW_IMAGE_MODEL", "").strip()

    @property
    def expects_gateway(self) -> bool:
        return True

    @property
    def transcript_container_path(self) -> str:
        return "/root/.openclaw/agents/main/sessions/chat.jsonl"

    def run_task(self, spec: AgentTaskSpec) -> AgentExecution:
        gateway_proc = None
        agent_proc = None
        elapsed_time = float(spec.timeout_seconds)

        try:
            gateway_port = self.gateway_port or available_gateway_port()
            exec_path = os.path.join(spec.workspace_path, "exec")
            tmp_path = os.path.join(spec.workspace_path, "tmp")
            os.makedirs(exec_path, exist_ok=True)

            start_container(
                spec.task_id,
                exec_path,
                extra_env=spec.task.get("env", ""),
                tmp_path=tmp_path,
                lobster_env=spec.lobster.get("env") if spec.lobster else None,
            )
            if spec.lobster:
                inject_lobster_workspace(spec.task_id, spec.lobster["workspace"])

            setup_workspace(spec.task_id, thinking=spec.thinking)
            setup_skills(spec.task_id, spec.task.get("skills", ""), spec.task.get("skills_path", ""))
            # Mock services in task warmups intentionally run in the background.
            # Detach their stdio so docker exec returns instead of waiting on them.
            run_warmup(
                spec.task_id,
                spec.task.get("warmup", ""),
                detach_background=True,
            )

            if spec.models_config:
                inject_openclaw_models(
                    spec.task_id, spec.models_config, defaults=spec.openclaw_defaults
                )

            self._set_model(spec.task_id, spec.model)
            self._set_model_runtime_params(spec.task_id, spec.model)
            self._inject_openrouter_key(spec.task_id)
            image_model = self.image_model or spec.model
            self._set_image_model(spec.task_id, image_model)
            port_result = subprocess.run(
                ["docker", "exec", spec.task_id, "openclaw", "config", "set", "gateway.port", str(gateway_port)],
                capture_output=True, text=True,
            )
            if port_result.returncode:
                raise RuntimeError(f"Gateway port configuration failed: {port_result.stderr}")

            gateway_proc = run_background(
                spec.task_id,
                bash_cmd=(
                    f"export OPENROUTER_API_KEY='{self.openrouter_api_key}' && "
                    f"export OPENROUTER_BASE_URL='{self.openrouter_base_url}' && "
                    f"openclaw gateway --port {gateway_port}"
                ),
                log_path=spec.output_dir / "gateway.log",
            )
            self._wait_for_gateway(spec.task_id, gateway_proc, gateway_port=gateway_port)
            (spec.output_dir / "gateway_runtime.json").write_text(
                json.dumps(dict(gateway_port=gateway_port, openclaw_defaults=spec.openclaw_defaults), indent=2)
            )

            safe_prompt = spec.prompt.replace("'", "'\\''")
            start_time = time.perf_counter()
            agent_proc = run_background(
                spec.task_id,
                bash_cmd=f"openclaw agent --session-id chat --timeout {spec.timeout_seconds} --message '{safe_prompt}'",
                log_path=spec.output_dir / "agent.log",
            )

            logger.info("[%s] Waiting for agent to finish...", spec.task_id)
            try:
                agent_proc.wait(timeout=spec.timeout_seconds)
                elapsed_time = time.perf_counter() - start_time
                logger.info(
                    "[%s] Agent finished successfully, elapsed: %.2f seconds",
                    spec.task_id,
                    elapsed_time,
                )
            except subprocess.TimeoutExpired:
                logger.info("[%s] Agent timed out...", spec.task_id)
                elapsed_time = float(spec.timeout_seconds)
                stop_background_process(agent_proc)

            logger.info("[%s] Agent exit code: %s", spec.task_id, agent_proc.returncode)
            return AgentExecution(
                elapsed_time=elapsed_time,
                error=None,
                gateway_proc=gateway_proc,
                agent_proc=agent_proc,
            )
        except Exception as exc:
            logger.error("[%s] Execution error: %s", spec.task_id, exc)
            return AgentExecution(
                elapsed_time=float(spec.timeout_seconds),
                error=str(exc),
                gateway_proc=gateway_proc,
                agent_proc=agent_proc,
            )
        except BaseException:
            # SIGTERM/cancellation must not orphan Enroot gateway descendants.
            for proc in (agent_proc, gateway_proc):
                if proc is not None:
                    stop_background_process(proc)
            raise

    def collect_usage(self, task_id: str, output_dir: Path, elapsed_time: float) -> dict:
        transcript_host = output_dir / "chat.jsonl"
        output_dir.mkdir(parents=True, exist_ok=True)
        r_cp = subprocess.run(
            ["docker", "cp", f"{task_id}:{self.transcript_container_path}", str(transcript_host)],
            capture_output=True,
            text=True,
        )
        if r_cp.returncode == 0 and transcript_host.exists():
            usage = extract_usage_from_jsonl(transcript_host)
            server_reported = int(usage.get("output_tokens", 0) or 0)
            locally_tokenized = locally_tokenized_assistant_output(transcript_host)
            usage["output_tokens_server_reported"] = server_reported
            usage["output_tokens_locally_tokenized"] = locally_tokenized
            if server_reported == 0 and locally_tokenized is not None:
                # Preserve the raw value above, while making the normal
                # per-task/aggregate output-token field useful for local vLLM.
                usage["output_tokens"] = locally_tokenized
                usage["token_accounting"] = "locally_tokenized_emitted_assistant_content"
        else:
            logger.warning("[%s] Transcript copy failed: %s", task_id, r_cp.stderr.strip())
            usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "request_count": 0,
            }
        usage["elapsed_time"] = round(elapsed_time, 2)
        return usage

    def _wait_for_gateway(self, task_id: str, proc: subprocess.Popen, *, gateway_port: int | None = None) -> None:
        """Wait inside the sandbox network, not a fixed startup sleep."""
        port = self.gateway_port if gateway_port is None else gateway_port
        probe = (
            "import socket; "
            f"s=socket.create_connection(('127.0.0.1', {port}), timeout=0.5); s.close()"
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("OpenClaw gateway exited before readiness; inspect gateway.log")
            result = subprocess.run(
                ["docker", "exec", task_id, "python3", "-c", probe],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return
            time.sleep(0.25)
        raise RuntimeError("OpenClaw gateway readiness timed out; inspect gateway.log")

    def _set_model(self, task_id: str, model: str) -> None:
        r = subprocess.run(
            ["docker", "exec", task_id, "/bin/bash", "-c", f"openclaw models set '{model}'"],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Model setup failed:\n{r.stderr}")
        logger.info("[%s] Model set: %s", task_id, model)

    def _set_model_runtime_params(self, task_id: str, model: str) -> None:
        """Apply optional per-model OpenClaw parameters without changing defaults."""
        raw_params = os.environ.get("WILDCLAW_OPENCLAW_MODEL_PARAMS", "").strip()
        raw_thinking = os.environ.get("WILDCLAW_OPENCLAW_THINKING", "").strip()
        if not raw_params and not raw_thinking:
            return
        try:
            params = json.loads(raw_params) if raw_params else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid WILDCLAW_OPENCLAW_MODEL_PARAMS JSON: {exc}") from exc
        if not isinstance(params, dict):
            raise RuntimeError("WILDCLAW_OPENCLAW_MODEL_PARAMS must be a JSON object")

        payload = json.dumps({"model": model, "params": params, "thinking": raw_thinking})
        inject_cmd = f"""python3 - <<'PY'
import json
import pathlib

payload = json.loads({payload!r})
path = pathlib.Path('/root/.openclaw/openclaw.json')
config = json.loads(path.read_text()) if path.exists() else {{}}
defaults = config.setdefault('agents', {{}}).setdefault('defaults', {{}})
entries = defaults.setdefault('models', {{}})
entry = entries.setdefault(payload['model'], {{}})
entry['params'] = payload['params']
if payload['thinking']:
    # This OpenClaw version accepts thinkingDefault at the agent-default level,
    # but rejects a per-model ``thinking`` key.  Qwen's request-level
    # chat_template_kwargs still explicitly keeps native thinking enabled.
    defaults['thinkingDefault'] = payload['thinking']
path.write_text(json.dumps(config, indent=2))
PY"""
        r = subprocess.run(
            ["docker", "exec", task_id, "/bin/bash", "-c", inject_cmd],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Model runtime-parameter setup failed:\n{r.stderr}")
        logger.info("[%s] Applied explicit model runtime params", task_id)

    def _inject_openrouter_key(self, task_id: str) -> None:
        if not self.openrouter_api_key:
            return

        auth_profile_path = "/root/.openclaw/agents/main/agent/auth-profiles.json"
        inject_cmd = f"""python3 - <<'PY'
import json
import pathlib

p = pathlib.Path("{auth_profile_path}")
d = json.loads(p.read_text()) if p.exists() else {{"version": 1, "profiles": {{}}}}
d.setdefault("profiles", {{}})["openrouter:default"] = {{
    "type": "api_key",
    "provider": "openrouter",
    "key": {json.dumps(self.openrouter_api_key)}
}}
p.write_text(json.dumps(d, indent=2))
PY"""
        subprocess.run(
            ["docker", "exec", task_id, "/bin/bash", "-c", inject_cmd],
            capture_output=True,
            text=True,
        )
        logger.info("[%s] Injected OPENROUTER_API_KEY into auth-profiles.json", task_id)

    def _set_image_model(self, task_id: str, model: str) -> None:
        subprocess.run(
            ["docker", "exec", task_id, "/bin/bash", "-c", f"openclaw config set agents.defaults.imageModel.primary '{model}'"],
            capture_output=True,
            text=True,
        )
        logger.info("[%s] imageModel set: %s", task_id, model)
