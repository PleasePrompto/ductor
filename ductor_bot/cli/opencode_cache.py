"""Persistent cache for opencode models with periodic refresh."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from ductor_bot.cli.model_cache import BaseModelCache
from ductor_bot.cli.opencode_discovery import (
    discover_opencode_default_model,
    discover_opencode_models,
    discover_opencode_recent_models,
)

# opencode model IDs always take the "<provider>/<model>" form.
_MODEL_SEPARATOR = "/"


@dataclass(frozen=True)
class OpencodeModelCache(BaseModelCache):
    """Immutable cache of opencode model IDs with refresh logic.

    No hardcoded fallback list: the available models depend on the providers
    the user has configured in opencode, so an empty discovery legitimately
    means "no models known yet".

    Besides the full model list, the cache carries the user's *default*
    opencode model (from the opencode config file) and the *recently used*
    models (from opencode's session database). Those are rediscovered on every
    load so they stay fresh, and are used by the model selector and by
    ``default_model_for_provider``.
    """

    last_updated: str  # ISO 8601 timestamp
    models: tuple[str, ...]
    default_model: str = ""
    recent_models: tuple[str, ...] = ()

    @classmethod
    def _provider_name(cls) -> str:
        return "OpenCode"

    @classmethod
    async def _discover(cls) -> tuple[str, ...]:
        return await discover_opencode_models()

    @classmethod
    def _empty_models(cls) -> tuple[str, ...]:
        return ()

    @classmethod
    async def load_or_refresh(
        cls,
        cache_path: Any,
        *,
        force_refresh: bool = False,
    ) -> Self:
        """Load models from disk/discovery and attach fresh default/recent state.

        The default and recently used models are rediscovered on every call —
        they are cheap local reads and keep the selector current between
        hourly cache refreshes.
        """
        cache = await super().load_or_refresh(cache_path, force_refresh=force_refresh)
        default_model, recent_models = await cls._discover_default_and_recent()
        return cls(
            last_updated=cache.last_updated,
            models=cache.models,
            default_model=default_model,
            recent_models=recent_models,
        )

    @classmethod
    async def _discover_default_and_recent(cls) -> tuple[str, tuple[str, ...]]:
        """Return ``(default_model, recent_models)`` from opencode's own state.

        When the config file sets no explicit default, the most recently used
        model acts as the user's default.
        """
        default_model = await discover_opencode_default_model()
        recent_models = await discover_opencode_recent_models()
        if not default_model and recent_models:
            default_model = recent_models[0]
        return default_model, recent_models

    def validate_model(self, model_id: str) -> bool:
        """Check if model exists in cache (or is any provider/model ID).

        opencode can run any ``<provider>/<model>`` its credentials support,
        so any slash-qualified ID passes; bare IDs are rejected because
        ``--model`` requires the qualified form.
        """
        return model_id in self.models or _MODEL_SEPARATOR in model_id

    def to_json(self) -> dict[str, Any]:
        """Serialize for persistence."""
        return {
            "last_updated": self.last_updated,
            "models": list(self.models),
            "default_model": self.default_model,
            "recent_models": list(self.recent_models),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        """Deserialize from JSON."""
        return cls(
            last_updated=data["last_updated"],
            models=tuple(data["models"]),
            default_model=data.get("default_model", ""),
            recent_models=tuple(data.get("recent_models", ())),
        )
