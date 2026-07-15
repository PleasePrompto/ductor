"""Redact environment assignments before command logging."""

from __future__ import annotations

import re

_ENV_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_VISIBLE_ENV_KEYS = {
    "DUCTOR_HOME",
    "DUCTOR_AGENT_NAME",
    "DUCTOR_TRANSPORT",
    "DUCTOR_CHAT_ID",
    "DUCTOR_TOPIC_ID",
    "DUCTOR_INTERAGENT_PORT",
    "DUCTOR_INTERAGENT_HOST",
    "DUCTOR_SHARED_MEMORY_PATH",
    "TZ",
    "PYTHONPATH",
    "HOME",
    "LANG",
    "CONTAINER",
    "PLAYWRIGHT_BROWSERS_PATH",
}


def _redact_assignment(value: str) -> str:
    match = _ENV_ASSIGNMENT.fullmatch(value)
    if match is None:
        return value
    key, raw_value = match.groups()
    if key in _VISIBLE_ENV_KEYS:
        return f"{key}={raw_value}"
    return f"{key}=***"


def redact_cmd_for_log(cmd: list[str]) -> list[str]:
    """Return a copy of *cmd* with environment values safe for logs."""
    redacted: list[str] = []
    for arg in cmd:
        if arg.startswith("--env="):
            redacted.append("--env=" + _redact_assignment(arg.removeprefix("--env=")))
        elif arg.startswith("-e="):
            redacted.append("-e=" + _redact_assignment(arg.removeprefix("-e=")))
        elif arg.startswith("-e") and arg != "-e" and "=" in arg[2:]:
            redacted.append("-e" + _redact_assignment(arg[2:]))
        else:
            redacted.append(_redact_assignment(arg))
    return redacted
