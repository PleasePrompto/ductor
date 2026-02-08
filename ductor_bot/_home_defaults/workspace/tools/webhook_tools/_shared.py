"""Shared helpers for webhook tool scripts."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

DUCTOR_HOME = Path(os.environ.get("DUCTOR_HOME", "~/.ductor")).expanduser()
HOOKS_PATH = DUCTOR_HOME / "webhooks.json"
CONFIG_PATH = DUCTOR_HOME / "config" / "config.json"
CRON_TASKS_DIR = DUCTOR_HOME / "workspace" / "cron_tasks"


def sanitize_name(raw: str) -> str:
    """Lowercase and normalize a hook name to [a-z0-9-]."""
    slug = raw.lower()
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


def load_hooks_or_default(hooks_path: Path) -> dict[str, Any]:
    """Load webhooks JSON or return an empty payload if missing/corrupt."""
    if not hooks_path.exists():
        return {"hooks": []}
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"hooks": []}
    if not isinstance(data, dict):
        return {"hooks": []}
    if not isinstance(data.get("hooks"), list):
        return {"hooks": []}
    return data


def load_hooks_strict(hooks_path: Path) -> dict[str, Any]:
    """Load webhooks JSON and raise on malformed structure."""
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), list):
        msg = "Corrupt webhooks.json -- cannot parse"
        raise TypeError(msg)
    return data


def save_hooks(hooks_path: Path, data: dict[str, Any]) -> None:
    """Persist webhooks JSON with stable formatting."""
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def available_hook_ids(hooks: list[dict[str, Any]]) -> list[str]:
    """Return all hook IDs for diagnostics."""
    return [str(h.get("id", "???")) for h in hooks]


def find_hook(hooks: list[dict[str, Any]], hook_id: str) -> dict[str, Any] | None:
    """Find a hook dict by ID."""
    return next((h for h in hooks if h.get("id") == hook_id), None)


def load_webhook_config() -> dict[str, Any]:
    """Load the webhooks section from config.json."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data.get("webhooks", {})
    except (json.JSONDecodeError, TypeError):
        return {}
