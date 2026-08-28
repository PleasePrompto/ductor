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
from ductor_bot.infra.base_observer import BaseObserver

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig, CleanupConfig
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 3600  # Re-check every hour whether it's time to run.
_STAGING_RETENTION_DAYS = 1


def _delete_old_files(directory: Path, max_age_days: int) -> int:
    """Delete files older than *max_age_days* from *directory*.

    Walks the directory tree recursively, removes old files, then prunes
    empty subdirectories so date-based folders (``YYYY-MM-DD/``) don't
    accumulate indefinitely.
    """
    if not directory.is_dir():
        return 0

    cutoff = time.time() - max_age_days * 86400
    deleted = 0
    for entry in directory.rglob("*"):
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                deleted += 1
        except OSError:
            logger.warning("Failed to delete %s", entry)

    # Prune empty subdirectories (bottom-up so nested empties are removed)
    for sub in sorted(directory.rglob("*"), reverse=True):
        if sub.is_dir():
            with contextlib.suppress(OSError):
                sub.rmdir()  # only succeeds if empty
    return deleted


class CleanupObserver(BaseObserver):
    """Runs daily file cleanup for telegram_files, output_to_user, and api_files.

    Follows the same lifecycle pattern as HeartbeatObserver:
    ``start()`` / ``stop()`` with an asyncio background task.
    """

    def __init__(self, config: AgentConfig, paths: DuctorPaths) -> None:
        super().__init__()
        self._config = config
        self._paths = paths
        self._last_run_date: str = ""

    @property
    def _cfg(self) -> CleanupConfig:
        return self._config.cleanup

    async def start(self) -> None:
        """Start the cleanup background loop."""
        if not self._cfg.enabled:
            logger.info("File cleanup disabled in config")
            return
        await super().start()
        logger.info(
            "File cleanup started (media: %dd, output: %dd, api: %dd, hour: %d:00)",
            self._cfg.media_files_days,
            self._cfg.output_to_user_days,
            self._cfg.api_files_days,
            self._cfg.check_hour,
        )

    async def stop(self) -> None:
        """Stop the cleanup background loop."""
        await super().stop()
        logger.info("File cleanup stopped")

    async def _run(self) -> None:
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

        await self._execute()
        # Set AFTER successful execution so a transient error doesn't
        # permanently suppress cleanup for the rest of the day.
        self._last_run_date = today

    async def _execute(self) -> None:
        """Perform the actual cleanup in a thread to avoid blocking the loop."""
        targets = [
            (self._paths.telegram_files_dir, self._cfg.media_files_days),
            (self._paths.output_to_user_dir, self._cfg.output_to_user_days),
            (self._paths.api_files_dir, self._cfg.api_files_days),
            (self._paths.matrix_files_dir, self._cfg.media_files_days),
            # Staged uploads are transient by design: anything still here
            # tomorrow is an upload the user walked away from.
            (self._paths.uploads_staging_dir, _STAGING_RETENTION_DAYS),
        ]
        results = await asyncio.to_thread(_run_cleanup, targets)

        if any(results):
            logger.info(
                "Cleanup complete: telegram=%d, output=%d, api=%d, matrix=%d, uploads=%d",
                results[0],
                results[1],
                results[2],
                results[3],
                results[4],
            )
        else:
            logger.debug("Cleanup: nothing to delete")


def _run_cleanup(targets: list[tuple[Path, int]]) -> list[int]:
    """Synchronous cleanup runner (called via ``asyncio.to_thread``)."""
    return [_delete_old_files(d, days) for d, days in targets]
