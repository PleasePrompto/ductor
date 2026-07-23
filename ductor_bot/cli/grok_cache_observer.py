"""Background observer for periodic Grok Build model cache refresh."""

from __future__ import annotations

from ductor_bot.cli.grok_cache import GrokModelCache
from ductor_bot.cli.model_cache import BaseModelCacheObserver


class GrokCacheObserver(BaseModelCacheObserver[GrokModelCache]):
    """Refreshes Grok Build model cache periodically.

    Loads initial cache at startup and refreshes every 60 minutes. Pass
    ``on_refresh`` (see base) to receive the model tuple after each load.
    """

    def _provider_name(self) -> str:
        return "Grok"

    async def _load_cache(self, *, initial: bool) -> GrokModelCache:
        return await GrokModelCache.load_or_refresh(self._cache_path, force_refresh=initial)
