"""Shared formatting primitives for command response text."""

from __future__ import annotations

from ductor_bot.i18n import t

SEP = "\u2500\u2500\u2500"

_SHELL_TOOLS = frozenset({"bash", "powershell", "cmd", "sh", "zsh", "shell"})
_TOOL_LABELS = {
    "toolsearch": "Search",
    "searchtool": "Search",
    "webfetch": "Web fetch",
    "websearch": "Web search",
}


def normalize_tool_name(name: str) -> str:
    """Normalize shell-related tool names to 'Shell' for display."""
    lower = name.lower()
    if lower in _SHELL_TOOLS:
        return "Shell"
    return _TOOL_LABELS.get(lower, name)


def fmt(*blocks: str) -> str:
    """Join non-empty blocks with double newlines."""
    return "\n\n".join(b for b in blocks if b)


# Known CLI error patterns -> user-friendly short explanation.
_AUTH_PATTERNS = (
    "401",
    "unauthorized",
    "authentication",
    "signing in again",
    "sign in again",
    "token has been",
)
_RATE_PATTERNS = (
    "429",
    "rate limit",
    "too many requests",
    "quota exceeded",
    # Codex wording for usage-cap exhaustion (e.g. "You've hit your usage limit. …").
    "usage limit",
    "upgrade to pro",
    "hit your",
)
_CONTEXT_PATTERNS = ("context length", "token limit", "maximum context", "too long")


def classify_cli_error(raw: str) -> str | None:
    """Return a user-facing hint for known CLI error patterns, or None."""
    lower = raw.lower()
    if any(p in lower for p in _AUTH_PATTERNS):
        return t("session.error_auth")
    if any(p in lower for p in _RATE_PATTERNS):
        return t("session.error_rate")
    if any(p in lower for p in _CONTEXT_PATTERNS):
        return t("session.error_context")
    return None


def session_error_text(model: str, cli_detail: str = "") -> str:
    """Build the error message shown to the user on CLI failure."""
    base = fmt(t("session.error_header"), SEP, t("session.error_body", model=model))
    hint = classify_cli_error(cli_detail) if cli_detail else None
    if hint:
        return fmt(base, t("session.error_cause", hint=hint))
    if cli_detail:
        # Show first meaningful line, truncated.
        detail = cli_detail.strip().split("\n")[0][:200]
        return fmt(base, t("session.error_detail", detail=detail))
    return base


def timeout_error_text(model: str, timeout_seconds: float) -> str:
    """Build the error message shown when the CLI times out."""
    minutes = int(timeout_seconds / 60)
    return fmt(
        t("timeout.error_header"), SEP, t("timeout.error_body", model=model, minutes=minutes)
    )


def new_session_text(provider: str) -> str:
    """Build /new response for provider-local reset."""
    provider_label = {
        "claude": "Claude",
        "codex": "Codex",
        "gemini": "Gemini",
        "antigravity": "Antigravity",
        "grok": "Grok Build",
    }.get(provider.lower(), provider)
    return fmt(
        t("session.reset_header"),
        SEP,
        t("session.reset_body", provider=provider_label),
    )


def stop_text(killed: bool, provider: str) -> str:
    """Build the /stop response."""
    body = t("stop.killed", provider=provider) if killed else t("stop.nothing")
    return fmt(t("stop.header"), SEP, body)


# -- Startup lifecycle messages --


def startup_notification_text(kind: str) -> str:
    """Notification text for startup events.

    Only ``first_start`` and ``system_reboot`` produce output.
    ``service_restart`` is silent (handled by the existing sentinel system).
    """
    if kind == "first_start":
        return fmt(t("startup.first_start_header"), SEP, t("startup.first_start_body"))
    if kind == "system_reboot":
        return fmt(t("startup.reboot_header"), SEP, t("startup.reboot_body"))
    return ""


# -- Auto-recovery messages --


def format_technical_footer(
    model_name: str,
    total_tokens: int,
    input_tokens: int,
    cost_usd: float,
    duration_ms: float | None,
) -> str:
    """Format technical metadata as a footer line."""
    output_tokens = total_tokens - input_tokens
    parts = [t("footer.model", name=model_name)]
    parts.append(t("footer.tokens", total=total_tokens, input=input_tokens, output=output_tokens))
    if cost_usd > 0:
        parts.append(t("footer.cost", cost=f"{cost_usd:.4f}"))
    if duration_ms is not None:
        secs = duration_ms / 1000
        parts.append(t("footer.time", secs=f"{secs:.1f}"))
    return "\n---\n" + " | ".join(parts)


def recovery_notification_text(
    kind: str,
    prompt_preview: str,
    session_name: str = "",
) -> str:
    """Notification that interrupted work is being recovered."""
    preview = prompt_preview[:80] + ("…" if len(prompt_preview) > 80 else "")
    if kind == "named_session":
        return fmt(
            t("recovery.named_header"),
            SEP,
            t("recovery.named_body", session=session_name, preview=preview),
        )
    return fmt(
        t("recovery.interrupted_header"),
        SEP,
        t("recovery.interrupted_body", preview=preview),
    )
