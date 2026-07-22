"""Persistent cache for Grok Build models with periodic refresh."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from ductor_bot.cli.grok_discovery import discover_grok_models
from ductor_bot.cli.model_cache import BaseModelCache
from ductor_bot.config import GROK_MODELS_ORDERED

# Hardcoded fallback when discovery and disk cache both fail.
_FALLBACK_GROK_MODELS: tuple[str, ...] = GROK_MODELS_ORDERED


@dataclass(frozen=True)
class GrokModelCache(BaseModelCache):
    """Immutable cache of Grok Build model IDs with refresh logic."""

    last_updated: str  # ISO 8601 timestamp
    models: tuple[str, ...]

    @classmethod
    def _provider_name(cls) -> str:
        return "Grok"

    @classmethod
    async def _discover(cls) -> tuple[str, ...]:
        return await discover_grok_models()

    @classmethod
    def _empty_models(cls) -> tuple[str, ...]:
        return ()

    @classmethod
    def _fallback_models(cls) -> tuple[str, ...]:
        return _FALLBACK_GROK_MODELS

    def validate_model(self, model_id: str) -> bool:
        """Check if model exists in cache (or is a grok-* ID)."""
        return model_id in self.models or model_id.startswith("grok-")

    def to_json(self) -> dict[str, Any]:
        """Serialize for persistence."""
        return {
            "last_updated": self.last_updated,
            "models": list(self.models),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        """Deserialize from JSON."""
        return cls(
            last_updated=data["last_updated"],
            models=tuple(data["models"]),
        )
