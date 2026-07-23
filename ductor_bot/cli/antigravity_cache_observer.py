"""Background observer for periodic Antigravity model cache refresh."""

from __future__ import annotations

from ductor_bot.cli.antigravity_cache import AntigravityModelCache
from ductor_bot.cli.model_cache import BaseModelCacheObserver


class AntigravityCacheObserver(BaseModelCacheObserver[AntigravityModelCache]):
    """Refreshes the Antigravity model cache periodically.

    Loads initial cache at startup and refreshes every 60 minutes. Pass
    ``on_refresh`` (see base) to receive the model tuple after each load.
    """

    def _provider_name(self) -> str:
        return "Antigravity"

    async def _load_cache(self, *, initial: bool) -> AntigravityModelCache:
        return await AntigravityModelCache.load_or_refresh(self._cache_path, force_refresh=initial)
