"""Shared helpers for webhook tool scripts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ductor_bot._home_defaults.workspace.tools._tool_shared import (
    available_ids,
    find_by_id,
    load_collection_or_default,
    load_collection_strict,
    sanitize_name,
    save_collection,
)

DUCTOR_HOME = Path(os.environ.get("DUCTOR_HOME", "~/.ductor")).expanduser()
HOOKS_PATH = DUCTOR_HOME / "webhooks.json"
CONFIG_PATH = DUCTOR_HOME / "config" / "config.json"
CRON_TASKS_DIR = DUCTOR_HOME / "workspace" / "cron_tasks"


def load_hooks_or_default(hooks_path: Path) -> dict[str, Any]:
    """Load webhooks JSON or return an empty payload if missing/corrupt."""
    return load_collection_or_default(hooks_path, "hooks")


def load_hooks_strict(hooks_path: Path) -> dict[str, Any]:
    """Load webhooks JSON and raise on malformed structure."""
    return load_collection_strict(hooks_path, "hooks")


def save_hooks(hooks_path: Path, data: dict[str, Any]) -> None:
    """Persist webhooks JSON with stable formatting."""
    save_collection(hooks_path, data)


def available_hook_ids(hooks: list[dict[str, Any]]) -> list[str]:
    """Return all hook IDs for diagnostics."""
    return available_ids(hooks)


def find_hook(hooks: list[dict[str, Any]], hook_id: str) -> dict[str, Any] | None:
    """Find a hook dict by ID."""
    return find_by_id(hooks, hook_id)


def reject_wake_overrides(
    mode: str,
    *,
    provider: str | None,
    model: str | None,
    reasoning_effort: str | None,
    cli_parameters: str | list[str] | None,
) -> str | None:
    """Return an error message when execution overrides are set on a wake hook.

    Wake resumes the live session: provider/model/effort/cli_parameters cannot
    apply there and were silently ignored before (#176). ``cron_task`` hooks
    run a fresh one-shot session and keep full override support.
    """
    if mode != "wake":
        return None
    offending = [
        flag
        for flag, value in (
            ("--provider", provider),
            ("--model", model),
            ("--reasoning-effort", reasoning_effort),
            ("--cli-parameters", cli_parameters),
        )
        if value
    ]
    if not offending:
        return None
    return (
        f"Execution overrides ({', '.join(offending)}) are not supported for mode 'wake' — "
        "a wake resumes the live session with its current provider/model. "
        "Use mode 'cron_task' for webhook-specific execution settings."
    )


def load_webhook_config() -> dict[str, Any]:
    """Load the webhooks section from config.json."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data.get("webhooks", {})
    except (json.JSONDecodeError, TypeError):
        return {}
