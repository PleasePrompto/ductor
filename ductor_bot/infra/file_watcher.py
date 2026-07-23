"""Reusable file-mtime poller for JSON config watchers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

logger = logging.getLogger(__name__)


class FileWatcher:
    """Poll a file's mtime and invoke a callback on change."""

    def __init__(
        self,
        path: Path,
        on_change: Callable[[], Awaitable[None]],
        interval: float = 5.0,
    ) -> None:
        self._path = path
        self._on_change = on_change
        self._interval = interval
        self._last_mtime: float = 0.0
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        """Begin polling."""
        self._running = True
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        """Cancel the polling task."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def update_mtime(self) -> None:
        """Snapshot the current mtime (avoids false-positive on next poll)."""
        try:
            self._last_mtime = await asyncio.to_thread(
                lambda: self._path.stat().st_mtime,
            )
        except FileNotFoundError:
            self._last_mtime = 0.0

    async def _watch_loop(self) -> None:
        """Poll file mtime at *interval* seconds, call *on_change* on delta."""
        while self._running:
            await asyncio.sleep(self._interval)
            try:
                current_mtime = await asyncio.to_thread(
                    lambda: self._path.stat().st_mtime,
                )
            except FileNotFoundError:
                continue
            if current_mtime != self._last_mtime:
                self._last_mtime = current_mtime
                await self._on_change()
