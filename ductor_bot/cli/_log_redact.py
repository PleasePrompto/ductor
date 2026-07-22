"""Redact environment assignments before command logging."""

from __future__ import annotations

_VISIBLE_ENV_KEYS = {
    "DUCTOR_HOME",
    "DUCTOR_AGENT_NAME",
    "DUCTOR_TRANSPORT",
    "DUCTOR_CHAT_ID",
    "DUCTOR_TOPIC_ID",
    "DUCTOR_TASK_ID",
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


def _redact_assignment(value: str, *, force: bool = False) -> str:
    key, separator, raw_value = value.partition("=")
    if not separator:
        return value
    if not force and (value.startswith("-") or " " in key or "/" in key):
        return value
    if key in _VISIBLE_ENV_KEYS:
        return f"{key}={raw_value}"
    return f"{key}=***"


def redact_cmd_for_log(cmd: list[str]) -> list[str]:
    """Return a copy of *cmd* with environment values safe for logs."""
    redacted: list[str] = []
    expects_env = False
    for arg in cmd:
        if expects_env:
            redacted.append(_redact_assignment(arg, force=True))
            expects_env = False
        elif arg in {"-e", "--env"}:
            redacted.append(arg)
            expects_env = True
        elif arg.startswith("--env="):
            redacted.append("--env=" + _redact_assignment(arg.removeprefix("--env="), force=True))
        elif arg.startswith("-e="):
            redacted.append("-e=" + _redact_assignment(arg.removeprefix("-e="), force=True))
        elif arg.startswith("-e") and arg != "-e" and "=" in arg[2:]:
            redacted.append("-e" + _redact_assignment(arg[2:], force=True))
        else:
            redacted.append(_redact_assignment(arg))
    return redacted
