"""Base classes for provider model-cache persistence and periodic refresh."""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Generic, Self, TypeVar

from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.infra.base_observer import BaseObserver

logger = logging.getLogger(__name__)

_CACHE_MAX_AGE = timedelta(hours=24)
REFRESH_INTERVAL_S: int = 3600


class BaseModelCache(ABC):
    """Abstract base for immutable model caches with disk persistence.

    Subclasses must be frozen dataclasses with ``last_updated: str`` and a
    ``models`` field (any sequence type).  They must implement the four
    abstract hooks below plus ``to_json`` / ``from_json``.
    """

    last_updated: str
    models: Any

    @classmethod
    @abstractmethod
    def _provider_name(cls) -> str:
        """Short label for log messages (e.g. ``"Codex"``)."""

    @classmethod
    @abstractmethod
    async def _discover(cls) -> Any:
        """Run provider-specific model discovery and return the models value."""

    @classmethod
    @abstractmethod
    def _empty_models(cls) -> Any:
        """Return the empty sentinel for the models field (e.g. ``[]`` or ``()``)."""

    @classmethod
    def _fallback_models(cls) -> Any:
        """Hardcoded fallback models when discovery and disk cache both fail.

        Override in subclasses to provide provider-specific defaults.
        Returns ``_empty_models()`` by default (no fallback).
        """
        return cls._empty_models()

    @abstractmethod
    def to_json(self) -> dict[str, Any]: ...

    @classmethod
    @abstractmethod
    def from_json(cls, data: dict[str, Any]) -> Self: ...

    @classmethod
    async def load_or_refresh(
        cls,
        cache_path: Path,
        *,
        force_refresh: bool = False,
    ) -> Self:
        """Load from disk, refresh if stale (>24 h) or missing.

        Args:
            cache_path: Path to JSON cache file.
            force_refresh: If True, ignore on-disk cache and rediscover models.

        Returns:
            Cache instance (possibly refreshed).
        """
        name = cls._provider_name()

        if force_refresh:
            logger.info("%s cache refresh forced", name)
            return await cls._refresh_and_save(cache_path)

        exists = await asyncio.to_thread(cache_path.exists)
        if exists:
            try:
                content = await asyncio.to_thread(cache_path.read_text)
                data = json.loads(content)
                cache = cls.from_json(data)

                last_updated = datetime.fromisoformat(cache.last_updated)
                age = datetime.now(UTC) - last_updated

                if age < _CACHE_MAX_AGE:
                    if cache.models:
                        logger.debug("%s cache is fresh, using cached models", name)
                        return cache

                    logger.info("%s cache is fresh but empty, forcing refresh", name)
                else:
                    logger.info("%s cache is stale (age: %s), refreshing", name, age)
            except Exception:
                logger.warning("Failed to load %s cache, will refresh", name, exc_info=True)

        return await cls._refresh_and_save(cache_path)

    @classmethod
    async def _refresh_and_save(cls, cache_path: Path) -> Self:
        """Discover models and save to disk.

        Never overwrites a non-empty on-disk cache with an empty result.
        Falls back to hardcoded defaults when both discovery and disk fail.
        Fallback models are returned in-memory only — NOT persisted — so
        that the next successful discovery replaces them automatically.
        """
        name = cls._provider_name()

        try:
            models = await cls._discover()
            model_count = len(models) if isinstance(models, Sequence) else 0
            logger.info("Discovered %d %s models", model_count, name)
        except Exception:
            logger.exception("Failed to discover %s models", name)
            models = cls._empty_models()

        # Discovery returned real models — persist and return.
        if models:
            cache = cls(  # type: ignore[call-arg]
                last_updated=datetime.now(UTC).isoformat(),
                models=models,
            )
            try:
                content = json.dumps(cache.to_json(), indent=2)
                await asyncio.to_thread(atomic_text_save, cache_path, content)
                logger.debug("Saved %s cache to %s", name, cache_path)
            except Exception:
                logger.exception("Failed to save %s cache to disk", name)
            return cache

        # Discovery empty — try to preserve existing non-empty disk cache.
        existing = await cls._try_load_disk(cache_path)
        if existing is not None and existing.models:
            count = len(existing.models) if isinstance(existing.models, Sequence) else 0
            logger.warning(
                "Discovery returned no %s models; keeping existing cache (%d models)",
                name,
                count,
            )
            return existing

        # Last resort: hardcoded fallback, in-memory only (NOT saved to disk).
        fallback = cls._fallback_models()
        if fallback:
            logger.warning(
                "Using hardcoded %s fallback models (not persisted)",
                name,
            )
            return cls(  # type: ignore[call-arg]
                last_updated=datetime.now(UTC).isoformat(),
                models=fallback,
            )

        # Truly empty — no discovery, no disk cache, no fallback.
        return cls(  # type: ignore[call-arg]
            last_updated=datetime.now(UTC).isoformat(),
            models=cls._empty_models(),
        )

    @classmethod
    async def _try_load_disk(cls, cache_path: Path) -> Self | None:
        """Try to load existing cache from disk. Returns ``None`` on failure."""
        try:
            if not await asyncio.to_thread(cache_path.exists):
                return None
            content = await asyncio.to_thread(cache_path.read_text)
            return cls.from_json(json.loads(content))
        except Exception:
            return None


CacheT = TypeVar("CacheT", bound=BaseModelCache)


class BaseModelCacheObserver(BaseObserver, ABC, Generic[CacheT]):
    """Abstract base for periodic model-cache refresh observers.

    Subclasses implement ``_provider_name`` and ``_load_cache`` and specialise
    the generic over their concrete :class:`BaseModelCache` type. Model count,
    last-updated timestamp, cache access, and the optional ``on_refresh``
    callback are handled here.
    """

    def __init__(
        self,
        cache_path: Path,
        *,
        on_refresh: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        """Initialize observer with cache file path.

        Args:
            cache_path: Path to JSON cache file.
            on_refresh: Optional callback invoked with the model list after
                each successful cache load/refresh.
        """
        super().__init__()
        self._cache_path = cache_path
        self._cache: CacheT | None = None
        self._on_refresh = on_refresh

    @abstractmethod
    def _provider_name(self) -> str:
        """Short label for log messages (e.g. ``"Codex"``)."""

    @abstractmethod
    async def _load_cache(self, *, initial: bool) -> CacheT:
        """Load or refresh the cache. *initial* is True on first call."""

    def _model_count(self) -> int:
        return len(self._cache.models) if self._cache else 0

    def _last_updated(self) -> str:
        return self._cache.last_updated if self._cache else ""

    def _on_cache_loaded(self) -> None:
        """Invoke the ``on_refresh`` callback when the cache holds models."""
        if self._on_refresh and self._cache and self._cache.models:
            self._on_refresh(self._cache.models)

    async def start(self) -> None:
        """Load initial cache and start refresh loop."""
        name = self._provider_name()
        obs = f"{name}CacheObserver"
        logger.info("%s starting, cache_path=%s", obs, self._cache_path)
        self._cache = await self._load_cache(initial=True)
        self._on_cache_loaded()
        logger.info(
            "%s cache loaded: %d models, last_updated=%s",
            name,
            self._model_count(),
            self._last_updated(),
        )
        await super().start()

    async def stop(self) -> None:
        """Stop refresh loop."""
        logger.info("%sCacheObserver stopping", self._provider_name())
        await super().stop()

    def get_cache(self) -> CacheT | None:
        """Return current cache (may be None if never loaded)."""
        return self._cache

    async def _run(self) -> None:
        """Refresh cache every 60 minutes."""
        name = self._provider_name()
        obs = f"{name}CacheObserver"
        try:
            while self._running:
                await asyncio.sleep(REFRESH_INTERVAL_S)
                if not self._running:
                    break  # type: ignore[unreachable]
                try:
                    logger.info("%s: refreshing cache", obs)
                    self._cache = await self._load_cache(initial=False)
                    self._on_cache_loaded()
                    logger.info(
                        "%s cache refreshed: %d models",
                        name,
                        self._model_count(),
                    )
                except Exception:
                    logger.exception("%s cache refresh failed, will retry in 60 minutes", name)
        except asyncio.CancelledError:
            logger.debug("%s refresh loop cancelled", obs)
            raise
