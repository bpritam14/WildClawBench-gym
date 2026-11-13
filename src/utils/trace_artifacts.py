"""Persist a portable per-task record of model reasoning, actions and output."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(part.get("text", part.get("thinking", ""))) if isinstance(part, dict) else _text(part) for part in value)
    return ""


def _token_count(text: str) -> int | None:
    """Tokenize emitted assistant content from the checkpoint's tokenizer.json."""
    tokenizer_path = os.environ.get("WILDCLAW_TOKENIZER_PATH", "").strip()
    if not tokenizer_path or not text:
        return None
    try:
        from tokenizers import Tokenizer
        tokenizer_json = Path(tokenizer_path) / "tokenizer.json"
        tokenizer = Tokenizer.from_file(str(tokenizer_json))
        return len(tokenizer.encode(text).ids)
    except Exception:
        return None


def locally_tokenized_assistant_output(transcript: Path) -> int | None:
    """Count all emitted assistant content in a persisted OpenClaw transcript.

    Local vLLM/OpenClaw deployments can legitimately return zero-valued usage
    blocks.  This fallback deliberately includes reasoning, tool calls, and
    visible text: all are tokens emitted by the evaluated model.
    """
    if not transcript.is_file():
        return None
    emitted_parts: list[str] = []
    for line in transcript.read_text(encoding="utf-8", errors="replace").split("\n"):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "message":
            continue
        message = event.get("message", {})
        if message.get("role") != "assistant":
            continue
        for part in message.get("content", []):
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "thinking":
                emitted_parts.append(str(part.get("thinking", "")))
            elif kind == "toolCall":
                emitted_parts.append(json.dumps(part, ensure_ascii=False, sort_keys=True))
            elif kind == "text":
                emitted_parts.append(str(part.get("text", "")))
    return _token_count("\n".join(emitted_parts))


def write_trace_artifact(output_dir: Path, task_id: str, usage: dict[str, Any]) -> None:
    """Write trace.json without changing the raw chat.jsonl retained by the runner."""
    transcript = output_dir / "chat.jsonl"
    messages: list[dict[str, Any]] = []
    if transcript.is_file():
        for line in transcript.read_text(encoding="utf-8", errors="replace").split("\n"):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "message":
                messages.append(event.get("message", {}))

    reasoning: list[str] = []
    final_output: list[str] = []
    actions: list[dict[str, Any]] = []
    emitted_parts: list[str] = []
    reported_output_tokens = 0
    for message in messages:
        if message.get("role") != "assistant":
            continue
        message_usage = message.get("usage") or {}
        reported_output_tokens += int(message_usage.get("output", 0) or 0)
        for part in message.get("content", []):
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "thinking":
                text = str(part.get("thinking", ""))
                reasoning.append(text)
                emitted_parts.append(text)
            elif kind == "toolCall":
                actions.append({
                    "id": part.get("id"), "name": part.get("name"),
                    "arguments": part.get("arguments", {}),
                })
                emitted_parts.append(json.dumps(part, ensure_ascii=False, sort_keys=True))
            elif kind == "text":
                text = str(part.get("text", ""))
                final_output.append(text)
                emitted_parts.append(text)

    emitted_text = "\n".join(emitted_parts)
    trace = {
        "task_id": task_id,
        "raw_transcript": "chat.jsonl" if transcript.is_file() else None,
        "reasoning": reasoning,
        "reasoning_tokens": {
            "locally_tokenized_emitted_reasoning": _token_count("\n".join(reasoning)),
            "server_reported_vllm": (usage.get("upstream_vllm_usage") or {}).get("reasoning_tokens"),
        },
        "action_steps": actions,
        "final_output": "\n".join(final_output).strip(),
        "output_tokens": {
            "server_reported": reported_output_tokens,
            "server_reported_vllm": (usage.get("upstream_vllm_usage") or {}).get("output_tokens"),
            "locally_tokenized_emitted_content": _token_count(emitted_text),
            "runner_total_output_tokens": usage.get("output_tokens", 0),
            "note": "Local count covers emitted reasoning, tool calls, and text; raw server counts are retained when supplied.",
        },
    }
    (output_dir / "trace.json").write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
