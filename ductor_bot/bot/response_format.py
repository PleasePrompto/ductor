"""Shared formatting primitives for command response text."""

from __future__ import annotations

SEP = "\u2500\u2500\u2500"


def fmt(*blocks: str) -> str:
    """Join non-empty blocks with double newlines."""
    return "\n\n".join(b for b in blocks if b)


# -- Shared response texts (eliminate duplication between handlers.py / commands.py) --

SESSION_ERROR_TEXT = fmt(
    "**Session Error**",
    SEP,
    "[{model}] An error occurred.\n"
    "Your session has been preserved -- send another message to retry.\n"
    "Use /new to start a fresh session if the problem persists.",
)


def new_session_text(provider: str) -> str:
    """Build /new response for provider-local reset."""
    provider_label = {"claude": "Claude", "codex": "Codex"}.get(provider.lower(), provider)
    return fmt(
        "**Session Reset**",
        SEP,
        f"Session reset for {provider_label} in this chat only.\n"
        "Other provider sessions were preserved.\n"
        "Send a message to continue.",
    )


def stop_text(killed: bool, provider: str) -> str:
    """Build the /stop response."""
    if killed:
        body = f"{provider} terminated. All queued messages discarded."
    else:
        body = "Nothing running right now."
    return fmt("**Agent Stopped**", SEP, body)
