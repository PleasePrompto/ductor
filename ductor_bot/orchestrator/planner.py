"""Sticky planner-mode helpers for Codex sessions."""

from __future__ import annotations

CODEX_PLANNER_APPEND_PROMPT = """Planner mode is active.
Stay planning-oriented and concise.
Ask short clarification questions when needed.
Do not start implementation unless explicitly instructed with /implement.
When offering options, first ask one explicit question or instruction sentence immediately before them.
Make the [button:...] suggestions direct answers to that question.
Do not emit standalone [button:...] suggestions without adjacent explanatory text."""


def planner_append_prompt(provider: str) -> str | None:
    """Return the planner overlay for providers that support it."""
    if provider == "codex":
        return CODEX_PLANNER_APPEND_PROMPT
    return None
