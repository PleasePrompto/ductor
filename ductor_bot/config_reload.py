"""Centralized config hot-reload: watch config.json and apply safe changes at runtime.

Mtime-based watcher (5-second poll) that detects config file changes, validates
the new config via Pydantic, diffs against the current config, and applies
hot-reloadable fields without restart. Fields requiring restart are logged as
warnings.

Usage::

    reloader = ConfigReloader(config_path, current_config, on_hot_reload, on_restart_needed)
    await reloader.start()
    ...
    await reloader.stop()
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ductor_bot.config import AgentConfig

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 5.0

# Fields that can be applied at runtime without restart.
_HOT_RELOADABLE: frozenset[str] = frozenset(
    {
        "model",
        "provider",
        "reasoning_effort",
        "cli_timeout",
        "max_budget_usd",
        "max_turns",
        "max_session_messages",
        "idle_timeout_minutes",
        "session_age_warning_hours",
        "daily_reset_hour",
        "daily_reset_enabled",
        "permission_mode",
        "file_access",
        "user_timezone",
        "streaming",
        "heartbeat",
        "cleanup",
        "cli_parameters",
        "project_roots",
        "allowed_user_ids",
        "allowed_group_ids",
        "group_mention_only",
        "scene",
        "image",
        "language",
    }
)


def diff_configs(old: AgentConfig, new: AgentConfig) -> dict[str, tuple[Any, Any]]:
    """Compare top-level fields. Returns ``{field: (old_val, new_val)}`` for changes."""
    old_dump = old.model_dump(mode="json")
    new_dump = new.model_dump(mode="json")
    changes: dict[str, tuple[Any, Any]] = {}
    for key in old_dump:
        if old_dump[key] != new_dump.get(key):
            changes[key] = (old_dump[key], new_dump[key])
    for key in new_dump:
        if key not in old_dump:
            changes[key] = (None, new_dump[key])
    return changes


def classify_changes(
    changes: dict[str, tuple[Any, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Split changes into hot-reloadable values and restart-required field names.

    Any field not in ``_HOT_RELOADABLE`` requires a restart — this fails safe
    for new/unknown config fields.

    Returns ``(hot_values, restart_fields)`` where ``hot_values`` maps field
    names to their new values and ``restart_fields`` lists fields that need
    a restart.
    """
    hot: dict[str, Any] = {}
    restart: list[str] = []
    for field, (_old, new_val) in changes.items():
        if field in _HOT_RELOADABLE:
            hot[field] = new_val
        else:
            restart.append(field)
    return hot, restart


class ConfigReloader:
    """Watch ``config.json`` for changes and apply hot-reloadable fields.

    Callbacks:
    - ``on_hot_reload(config, hot_fields)`` — called with the updated config
      and a dict of ``{field_name: new_value}`` for hot-reloaded fields.
    - ``on_restart_needed(fields)`` — called with a list of field names that
      require a restart.
    """

    def __init__(
        self,
        config_path: Path,
        current_config: AgentConfig,
        *,
        on_hot_reload: Callable[[AgentConfig, dict[str, Any]], None] | None = None,
        on_restart_needed: Callable[[list[str]], None] | None = None,
    ) -> None:
        self._path = config_path
        self._config = current_config
        self._on_hot_reload = on_hot_reload
        self._on_restart_needed = on_restart_needed
        self._last_mtime: float = 0.0
        self._task: asyncio.Task[None] | None = None
        self._init_mtime()

    def _init_mtime(self) -> None:
        """Read the initial mtime so we don't trigger on first poll."""
        try:
            self._last_mtime = self._path.stat().st_mtime
        except OSError:
            self._last_mtime = 0.0

    async def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Config reloader started watching %s", self._path)

    async def stop(self) -> None:
        """Stop the background polling task."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("Config reloader stopped")

    async def _poll_loop(self) -> None:
        """Poll config file mtime and trigger reload on change."""
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            await self._check()

    async def _check(self) -> None:
        """Check mtime, load, diff, and apply if changed."""
        try:
            stat = self._path.stat()
        except OSError:
            return

        if stat.st_mtime <= self._last_mtime:
            return

        self._last_mtime = stat.st_mtime
        logger.info("Config file changed, reloading...")

        new_config = await self._load_config()
        if new_config is None:
            return

        changes = diff_configs(self._config, new_config)
        if not changes:
            logger.debug("Config file changed but no field differences detected")
            return

        hot, restart = classify_changes(changes)

        if hot:
            self._apply_hot(new_config, hot)

        if restart and self._on_restart_needed:
            self._on_restart_needed(restart)

    async def _load_config(self) -> AgentConfig | None:
        """Load and validate config.json. Returns None on error."""
        try:
            raw = await asyncio.to_thread(self._path.read_text, "utf-8")
            data = json.loads(raw)
            return AgentConfig(**data)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Config reload failed (file error): %s", exc)
            return None
        except ValidationError as exc:
            logger.warning("Config reload failed (validation): %s", exc)
            return None

    def _apply_hot(self, new_config: AgentConfig, hot: dict[str, Any]) -> None:
        """Apply hot-reloadable fields to the current config."""
        for field in hot:
            setattr(self._config, field, getattr(new_config, field))

        logger.info("Config hot-reloaded: %s", ", ".join(sorted(hot)))

        if self._on_hot_reload:
            self._on_hot_reload(self._config, hot)
