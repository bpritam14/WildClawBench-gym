#!/usr/bin/env python3
"""Small Docker CLI compatibility layer for WildClawBench on Enroot.

Only the ``run``, ``exec``, ``cp``, and ``rm`` forms used by this repository
are implemented. Each logical Docker container maps to a writable named Enroot
container, while background ``docker exec`` calls become ordinary concurrent
``enroot start`` processes sharing that root filesystem.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import NoReturn


def fail(message: str, code: int = 2) -> NoReturn:
    print(f"wildclaw enroot docker shim: {message}", file=sys.stderr)
    raise SystemExit(code)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        fail(f"{name} must be set")
    return value


STATE_DIR = Path(required_env("WILDCLAW_ENROOT_STATE_DIR"))
ENROOT_IMAGE = Path(required_env("WILDCLAW_ENROOT_IMAGE"))
ENROOT_DATA_PATH = Path(required_env("ENROOT_DATA_PATH"))


def state_path(name: str) -> Path:
    if not name or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in name):
        fail(f"invalid container name: {name!r}")
    return STATE_DIR / f"{name}.json"


def load_state(name: str) -> dict:
    path = state_path(name)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"container does not exist: {name}", 1)


def save_state(name: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = state_path(name)
    path.write_text(json.dumps(state), encoding="utf-8")
    path.chmod(0o600)


def container_path(name: str, value: str) -> Path:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        fail(f"unsafe container path: {value!r}")
    return ENROOT_DATA_PATH / name / Path(*path.parts[1:])


def enroot_start_args(name: str, extra_env: list[str] | None = None) -> list[str]:
    state = load_state(name)
    args = ["enroot", "start", "--root", "--rw"]
    for mount in state.get("mounts", []):
        args += ["--mount", mount]
    for item in [*state.get("env", []), *(extra_env or [])]:
        args += ["--env", item]
    args.append(name)
    return args


def docker_volume_to_enroot(value: str) -> str:
    """Translate Docker's SOURCE:TARGET[:MODE] into Enroot fstab fields."""
    parts = value.split(":")
    if len(parts) not in (2, 3):
        fail(f"unsupported docker volume: {value!r}")
    source_value, target_value = parts[:2]
    mode = parts[2] if len(parts) == 3 else "rw"
    source = Path(source_value)
    target = PurePosixPath(target_value)
    if not source.is_absolute() or not target.is_absolute() or ".." in target.parts:
        fail(f"volume paths must be absolute and safe: {value!r}")
    if not source.exists():
        fail(f"volume source does not exist: {source}")
    if mode not in ("ro", "rw"):
        fail(f"unsupported docker volume mode: {mode!r}")
    if any(char.isspace() for char in value):
        fail(f"Enroot cannot mount a path containing whitespace: {value!r}")
    create_kind = "dir" if source.is_dir() else "file"
    return f"{source}:{target}:none:x-create={create_kind},bind,{mode},nosuid"


def run_container(argv: list[str]) -> int:
    name = ""
    env: list[str] = []
    mounts: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-d", "--detach", "--rm"):
            index += 1
        elif arg in ("--name", "-e", "--env", "-v", "--volume", "--network", "-w", "--workdir"):
            if index + 1 >= len(argv):
                fail(f"missing value after {arg}")
            value = argv[index + 1]
            if arg == "--name":
                name = value
            elif arg in ("-e", "--env"):
                env.append(value)
            elif arg in ("-v", "--volume"):
                mounts.append(docker_volume_to_enroot(value))
            index += 2
        elif arg.startswith("-"):
            fail(f"unsupported docker run option: {arg}")
        else:
            break

    if not name:
        fail("docker run requires --name")
    if index >= len(argv):
        fail("docker run requires an image")
    if not ENROOT_IMAGE.is_file():
        fail(f"Enroot image not found: {ENROOT_IMAGE}", 1)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    create = subprocess.run(
        ["enroot", "create", "--name", name, str(ENROOT_IMAGE)],
        text=True,
        capture_output=True,
    )
    if create.returncode != 0:
        sys.stderr.write(create.stderr)
        return create.returncode

    save_state(name, {"env": env, "mounts": mounts})
    print(hashlib.sha256(name.encode()).hexdigest())
    return 0


def exec_container(argv: list[str]) -> int:
    extra_env: list[str] = []
    detach = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-d", "--detach"):
            detach = True
            index += 1
        elif arg in ("-e", "--env"):
            if index + 1 >= len(argv):
                fail(f"missing value after {arg}")
            extra_env.append(argv[index + 1])
            index += 2
        elif arg in ("-u", "--user", "-w", "--workdir"):
            if index + 1 >= len(argv):
                fail(f"missing value after {arg}")
            index += 2
        elif arg in ("-i", "--interactive", "-t", "--tty"):
            index += 1
        elif arg.startswith("-"):
            fail(f"unsupported docker exec option: {arg}")
        else:
            break

    if index + 1 >= len(argv):
        fail("docker exec requires a container and command")
    name = argv[index]
    command = argv[index + 1 :]
    args = [*enroot_start_args(name, extra_env), *command]
    if detach:
        # Match `docker exec -d`: return immediately while Enroot keeps the
        # service process attached to the named writable container filesystem.
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return 0
    os.execvpe(args[0], args, os.environ)


def split_copy_spec(value: str) -> tuple[str | None, str]:
    head, separator, tail = value.partition(":")
    if separator and head and "/" not in head:
        return head, tail
    return None, value


def copy_path(source: Path, destination: Path, copy_contents: bool) -> int:
    if not source.exists() and not source.is_symlink():
        print(f"docker cp: source does not exist: {source}", file=sys.stderr)
        return 1

    if copy_contents:
        destination.mkdir(parents=True, exist_ok=True)
        command = ["cp", "-a", f"{source}/.", str(destination)]
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = ["cp", "-a", str(source), str(destination)]
    return subprocess.run(command).returncode


def copy_files(argv: list[str]) -> int:
    if len(argv) != 2:
        fail("docker cp requires exactly two paths")
    source_container, source_value = split_copy_spec(argv[0])
    destination_container, destination_value = split_copy_spec(argv[1])
    if bool(source_container) == bool(destination_container):
        fail("docker cp requires exactly one container path")

    source_contents = source_value.rstrip().endswith("/.")
    source_value = source_value[:-2] if source_contents else source_value
    if source_container:
        load_state(source_container)
        source = container_path(source_container, source_value)
        destination = Path(destination_value)
    else:
        load_state(destination_container or "")
        source = Path(source_value)
        destination = container_path(destination_container or "", destination_value)
    return copy_path(source, destination, source_contents)


def remove_container(argv: list[str]) -> int:
    names = [arg for arg in argv if arg not in ("-f", "--force")]
    return_code = 0
    for name in names:
        result = subprocess.run(["enroot", "remove", "--force", name], capture_output=True)
        state_path(name).unlink(missing_ok=True)
        if result.returncode != 0:
            return_code = result.returncode
    return return_code


def main() -> int:
    if len(sys.argv) < 2:
        fail("missing command")
    command, argv = sys.argv[1], sys.argv[2:]
    if command == "run":
        return run_container(argv)
    if command == "exec":
        return exec_container(argv)
    if command == "cp":
        return copy_files(argv)
    if command == "rm":
        return remove_container(argv)
    fail(f"unsupported docker command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
