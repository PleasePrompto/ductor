"""Webhook management: JSON-based persistence.

Hooks are stored in a JSON file. The WebhookObserver watches the file
for changes and keeps the in-memory registry in sync.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ductor_bot.webhook.models import WebhookEntry

logger = logging.getLogger(__name__)


class WebhookManager:
    """Manages webhook hooks: JSON persistence.

    The WebhookObserver watches the JSON file for changes.
    This class is responsible for data only.
    """

    def __init__(self, *, hooks_path: Path) -> None:
        self._hooks_path = hooks_path
        self._hooks: list[WebhookEntry] = self._load()

    # -- CRUD --

    def add_hook(self, hook: WebhookEntry) -> None:
        """Add a new hook. Raises ValueError if ID already exists."""
        if any(h.id == hook.id for h in self._hooks):
            msg = f"Hook '{hook.id}' already exists"
            raise ValueError(msg)
        self._hooks.append(hook)
        self._save()
        logger.info("Webhook added: %s (mode=%s)", hook.id, hook.mode)

    def remove_hook(self, hook_id: str) -> bool:
        """Remove a hook by ID. Returns False if not found."""
        before = len(self._hooks)
        self._hooks = [h for h in self._hooks if h.id != hook_id]
        if len(self._hooks) == before:
            return False
        self._save()
        logger.info("Webhook removed: %s", hook_id)
        return True

    def list_hooks(self) -> list[WebhookEntry]:
        """Return all hooks."""
        return list(self._hooks)

    def get_hook(self, hook_id: str) -> WebhookEntry | None:
        """Return a hook by ID, or None."""
        return next((h for h in self._hooks if h.id == hook_id), None)

    def update_hook(self, hook_id: str, **updates: Any) -> bool:
        """Update fields on an existing hook. Returns False if not found."""
        hook = self.get_hook(hook_id)
        if hook is None:
            return False
        for key, value in updates.items():
            if hasattr(hook, key):
                setattr(hook, key, value)
        self._save()
        return True

    def record_trigger(self, hook_id: str, *, error: str | None = None) -> None:
        """Increment trigger_count, set last_triggered_at, optionally set last_error."""
        hook = self.get_hook(hook_id)
        if hook is None:
            return
        hook.trigger_count += 1
        hook.last_triggered_at = datetime.now(UTC).isoformat()
        hook.last_error = error
        self._save()

    def reload(self) -> None:
        """Re-read hooks from disk (called by WebhookObserver on file change)."""
        self._hooks = self._load()

    # -- Persistence --

    def _load(self) -> list[WebhookEntry]:
        """Load hooks from JSON file."""
        if not self._hooks_path.exists():
            return []
        try:
            data = json.loads(self._hooks_path.read_text(encoding="utf-8"))
            return [WebhookEntry.from_dict(h) for h in data.get("hooks", [])]
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Corrupt webhooks file: %s", self._hooks_path)
            return []

    def _save(self) -> None:
        """Save hooks to JSON file atomically (temp write + rename)."""
        self._hooks_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"hooks": [h.to_dict() for h in self._hooks]}
        content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        fd, tmp_path = tempfile.mkstemp(dir=str(self._hooks_path.parent), suffix=".tmp")
        tmp = Path(tmp_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            tmp.replace(self._hooks_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
