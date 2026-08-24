"""Background observer for periodic opencode model cache refresh."""

from __future__ import annotations

from ductor_bot.cli.model_cache import BaseModelCacheObserver
from ductor_bot.cli.opencode_cache import OpencodeModelCache
from ductor_bot.config import set_opencode_default_model, set_opencode_recent_models


class OpencodeCacheObserver(BaseModelCacheObserver[OpencodeModelCache]):
    """Refreshes opencode model cache periodically.

    Loads initial cache at startup and refreshes every 60 minutes. Pass
    ``on_refresh`` (see base) to receive the model tuple after each load.
    Also pushes the user's default and recently used opencode models into the
    runtime registry so the model selector can show them.
    """

    def _provider_name(self) -> str:
        return "OpenCode"

    async def _load_cache(self, *, initial: bool) -> OpencodeModelCache:
        return await OpencodeModelCache.load_or_refresh(self._cache_path, force_refresh=initial)

    def _on_cache_loaded(self) -> None:
        if self._cache is not None:
            set_opencode_default_model(self._cache.default_model)
            set_opencode_recent_models(self._cache.recent_models)
        super()._on_cache_loaded()
