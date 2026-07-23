"""Background observer for periodic Codex model cache refresh."""

from __future__ import annotations

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.model_cache import BaseModelCacheObserver


class CodexCacheObserver(BaseModelCacheObserver[CodexModelCache]):
    """Refreshes Codex model cache periodically.

    Loads initial cache at startup and refreshes every 60 minutes.
    """

    def _provider_name(self) -> str:
        return "Codex"

    async def _load_cache(self, *, initial: bool) -> CodexModelCache:
        return await CodexModelCache.load_or_refresh(self._cache_path, force_refresh=initial)
