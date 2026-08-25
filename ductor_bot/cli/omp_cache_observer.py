"""Periodic refresh observer for Oh My Pi model cache."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ductor_bot.cli.model_cache import BaseModelCacheObserver
from ductor_bot.cli.omp_cache import OmpModelCache


class OmpCacheObserver(BaseModelCacheObserver[OmpModelCache]):
    """Observer that refreshes :class:`OmpModelCache` hourly."""

    def _provider_name(self) -> str:
        return "Omp"

    async def _load_cache(self, *, initial: bool) -> OmpModelCache:
        return await OmpModelCache.load_or_refresh(self._cache_path, force_refresh=initial)

    def __init__(
        self,
        cache_path: Path,
        *,
        on_refresh: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        super().__init__(cache_path, on_refresh=on_refresh)
