"""Cleanup observer: daily removal of old files from workspace directories."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.config import resolve_user_timezone

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig, CleanupConfig
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 3600  # Re-check every hour whether it's time to run.


def _delete_old_files(directory: Path, max_age_days: int) -> int:
    """Delete files older than *max_age_days* from *directory*.

    Returns the number of deleted files.  Non-recursive on purpose:
    only top-level files are cleaned, subdirectories are left untouched.
    """
    if not directory.is_dir():
        return 0

    cutoff = time.time() - max_age_days * 86400
    deleted = 0
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                deleted += 1
        except OSError:
            logger.warning("Failed to delete %s", entry)
    return deleted


class CleanupObserver:
    """Runs daily file cleanup for telegram_files and output_to_user.

    Follows the same lifecycle pattern as HeartbeatObserver:
    ``start()`` / ``stop()`` with an asyncio background task.
    """

    def __init__(self, config: AgentConfig, paths: DuctorPaths) -> None:
        self._config = config
        self._paths = paths
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last_run_date: str = ""

    @property
    def _cfg(self) -> CleanupConfig:
        return self._config.cleanup

    async def start(self) -> None:
        """Start the cleanup background loop."""
        if not self._cfg.enabled:
            logger.info("File cleanup disabled in config")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(_log_task_crash)
        logger.info(
            "File cleanup started (telegram_files: %dd, output_to_user: %dd, check_hour: %d:00)",
            self._cfg.telegram_files_days,
            self._cfg.output_to_user_days,
            self._cfg.check_hour,
        )

    async def stop(self) -> None:
        """Stop the cleanup background loop."""
        self._running = False
        if self._task:
            task = self._task
            self._task = None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("File cleanup stopped")

    async def _loop(self) -> None:
        """Sleep -> check hour -> run if due -> repeat."""
        try:
            while self._running:
                await asyncio.sleep(_CHECK_INTERVAL)
                if not self._running or not self._cfg.enabled:
                    continue
                try:
                    await self._maybe_run()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Cleanup tick failed (continuing)")
        except asyncio.CancelledError:
            logger.debug("Cleanup loop cancelled")

    async def _maybe_run(self) -> None:
        """Run cleanup if the current hour matches and we haven't run today."""
        tz = resolve_user_timezone(self._config.user_timezone)
        now = datetime.now(tz)
        today = now.strftime("%Y-%m-%d")

        if now.hour != self._cfg.check_hour:
            return
        if self._last_run_date == today:
            return

        self._last_run_date = today
        await self._execute()

    async def _execute(self) -> None:
        """Perform the actual cleanup in a thread to avoid blocking the loop."""
        telegram_days = self._cfg.telegram_files_days
        output_days = self._cfg.output_to_user_days
        telegram_dir = self._paths.telegram_files_dir
        output_dir = self._paths.output_to_user_dir

        t_deleted, o_deleted = await asyncio.to_thread(
            _run_cleanup, telegram_dir, telegram_days, output_dir, output_days
        )

        if t_deleted or o_deleted:
            logger.info(
                "Cleanup complete: %d file(s) from telegram_files, %d from output_to_user",
                t_deleted,
                o_deleted,
            )
        else:
            logger.debug("Cleanup: nothing to delete")


def _run_cleanup(
    telegram_dir: Path,
    telegram_days: int,
    output_dir: Path,
    output_days: int,
) -> tuple[int, int]:
    """Synchronous cleanup runner (called via ``asyncio.to_thread``)."""
    t = _delete_old_files(telegram_dir, telegram_days)
    o = _delete_old_files(output_dir, output_days)
    return t, o


def _log_task_crash(task: asyncio.Task[None]) -> None:
    """Log if the cleanup background task crashes unexpectedly."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Cleanup loop crashed: %s", exc, exc_info=exc)
