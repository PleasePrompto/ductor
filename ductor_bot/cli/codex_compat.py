"""Compatibility helpers for local Codex CLI execution."""

from __future__ import annotations

from pathlib import Path
from shutil import which

CODEX_APP_BIN = Path("/Applications/Codex.app/Contents/Resources/codex")
_GPT_55_PREFIX = "gpt-5.5"
_OMIT_REASONING_EFFORTS = {"", "default", "none", "clear"}
_GPT_55_REASONING_ALIASES = {
    "fast": "low",
    "minimal": "low",
    "medium": "low",
    "flex": "high",
}


def find_codex_cli() -> str:
    """Return the preferred local Codex executable path."""
    if CODEX_APP_BIN.exists():
        return str(CODEX_APP_BIN)
    path = which("codex")
    if not path:
        msg = "codex CLI not found on PATH. Install via: npm install -g @openai/codex"
        raise FileNotFoundError(msg)
    return path


def normalize_codex_reasoning_effort(model: str | None, reasoning_effort: str | None) -> str:
    """Normalize legacy effort names to values accepted by the selected Codex model."""
    effort = str(reasoning_effort or "").strip().lower()
    if effort in _OMIT_REASONING_EFFORTS:
        return ""

    model_name = str(model or "").strip().lower()
    if model_name.startswith(_GPT_55_PREFIX):
        return _GPT_55_REASONING_ALIASES.get(effort, effort)
    return effort


def codex_reasoning_effort_for_command(
    model: str | None,
    reasoning_effort: str | None,
    *,
    omit_legacy_medium_default: bool = False,
) -> str:
    """Return the reasoning effort value to send on the CLI, or empty to omit it."""
    normalized = normalize_codex_reasoning_effort(model, reasoning_effort)
    if omit_legacy_medium_default and normalized == "medium":
        return ""
    return normalized
