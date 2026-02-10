"""Shared formatting primitives for command response text."""

from __future__ import annotations

SEP = "\u2500\u2500\u2500"


def fmt(*blocks: str) -> str:
    """Join non-empty blocks with double newlines."""
    return "\n\n".join(b for b in blocks if b)


# -- Shared response texts (eliminate duplication between handlers.py / commands.py) --

NEW_SESSION_TEXT = fmt(
    "**Session Reset**",
    SEP,
    "Everything cleared -- ready to go.\nSend a message to start your new session.",
)


def stop_text(killed: bool, provider: str) -> str:
    """Build the /stop response."""
    if killed:
        body = f"{provider} terminated. All queued messages discarded."
    else:
        body = "Nothing running right now."
    return fmt("**Agent Stopped**", SEP, body)
