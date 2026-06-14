"""Shared host runtime helpers for the Antigravity CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_SANDBOX_ENV_KEYS = frozenset({"CODEX_SANDBOX_NETWORK_DISABLED"})


def antigravity_process_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a host environment suitable for the official ``agy`` CLI."""
    env = dict(os.environ if base_env is None else base_env)
    for key in _SANDBOX_ENV_KEYS:
        env.pop(key, None)
    return env


def configured_antigravity_models(home: Path | None = None) -> tuple[str, ...]:
    """Return the model selected in the official Antigravity settings."""
    root = home or Path.home()
    settings = root / ".gemini" / "antigravity-cli" / "settings.json"
    data = _read_json(settings)
    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        return ()
    return (model.strip(),)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}
