"""Background observer for periodic Codex model cache refresh."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from ductor_bot.cli.codex_cache import CodexModelCache

logger = logging.getLogger(__name__)


class CodexCacheObserver:
    """Refreshes Codex model cache periodically.

    Loads initial cache at startup and refreshes every 60 minutes.
    """

    def __init__(self, cache_path: Path) -> None:
        """Initialize observer with cache file path."""
        self._cache_path = cache_path
        self._cache: CodexModelCache | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        """Load initial cache and start refresh loop."""
        logger.info("CodexCacheObserver starting, cache_path=%s", self._cache_path)
        self._cache = await CodexModelCache.load_or_refresh(self._cache_path)
        logger.info(
            "Codex cache loaded: %d models, last_updated=%s",
            len(self._cache.models),
            self._cache.last_updated,
        )
        self._running = True
        self._task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        """Stop refresh loop."""
        logger.info("CodexCacheObserver stopping")
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def get_cache(self) -> CodexModelCache | None:
        """Return current cache (may be None if never loaded)."""
        return self._cache

    async def _refresh_loop(self) -> None:
        """Refresh cache every 60 minutes."""
        try:
            while self._running:
                await asyncio.sleep(3600)  # 60 minutes
                if not self._running:
                    break  # type: ignore[unreachable]
                try:
                    logger.info("CodexCacheObserver: refreshing cache")
                    self._cache = await CodexModelCache.load_or_refresh(self._cache_path)
                    logger.info(
                        "Codex cache refreshed: %d models",
                        len(self._cache.models),
                    )
                except Exception:
                    logger.exception("Codex cache refresh failed, will retry in 60 minutes")
        except asyncio.CancelledError:
            logger.debug("CodexCacheObserver refresh loop cancelled")
            raise
