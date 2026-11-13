"""Shared runtime budgets and gateway isolation for native and Gym evaluation."""

from contextlib import ExitStack
import socket


def context_defaults(context_window: int, max_tokens: int) -> dict:
    """Reserve slack for binary tool text and enable single-turn compaction."""
    if max_tokens < 1 or context_window <= 4 * max_tokens:
        raise ValueError("Context window must exceed four completion budgets")
    working_budget = context_window - 4 * max_tokens
    reserve = min(4 * max_tokens, working_budget // 2)
    return dict(
        contextTokens=working_budget,
        compaction=dict(
            mode="default", reserveTokens=reserve, reserveTokensFloor=reserve,
            keepRecentTokens=min(4096, reserve // 2),
        ),
    )


def available_gateway_port(preferred: int = 0) -> int:
    """Probe gateway/browser ports without touching another listener's owner."""
    for _ in range(16):
        try:
            with ExitStack() as stack:
                listener = stack.enter_context(socket.socket())
                listener.bind(("127.0.0.1", preferred))
                port = listener.getsockname()[1]
                for companion in (port + 1, port + 2):
                    probe = stack.enter_context(socket.socket())
                    probe.bind(("127.0.0.1", companion))
                return port
        except (OSError, OverflowError):
            if preferred:
                raise RuntimeError(f"Requested gateway port range {preferred}..{preferred + 2} is occupied")
    raise RuntimeError("Could not find an available OpenClaw gateway/browser port range")
