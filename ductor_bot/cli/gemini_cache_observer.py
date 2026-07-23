"""Background observer for periodic Gemini model cache refresh."""

from __future__ import annotations

from ductor_bot.cli.gemini_cache import GeminiModelCache
from ductor_bot.cli.model_cache import BaseModelCacheObserver


class GeminiCacheObserver(BaseModelCacheObserver[GeminiModelCache]):
    """Refreshes Gemini model cache periodically.

    Loads initial cache at startup and refreshes every 60 minutes. Pass
    ``on_refresh`` (see base) to receive the model tuple after each load.
    """

    def _provider_name(self) -> str:
        return "Gemini"

    async def _load_cache(self, *, initial: bool) -> GeminiModelCache:
        return await GeminiModelCache.load_or_refresh(self._cache_path, force_refresh=initial)
