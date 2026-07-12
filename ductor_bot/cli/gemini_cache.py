"""Persistent cache for Gemini models with periodic refresh."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Self

from ductor_bot.cli.gemini_utils import discover_gemini_models
from ductor_bot.cli.model_cache import BaseModelCache

# Hardcoded fallback when discovery and disk cache both fail.
_FALLBACK_GEMINI_MODELS: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-3-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3.5-flash",
)


@dataclass(frozen=True)
class GeminiModelCache(BaseModelCache):
    """Immutable cache of Gemini model IDs with refresh logic."""

    last_updated: str  # ISO 8601 timestamp
    models: tuple[str, ...]

    @classmethod
    def _provider_name(cls) -> str:
        return "Gemini"

    @classmethod
    async def _discover(cls) -> tuple[str, ...]:
        discovered = await asyncio.to_thread(discover_gemini_models)
        return tuple(sorted(discovered))

    @classmethod
    def _empty_models(cls) -> tuple[str, ...]:
        return ()

    @classmethod
    def _fallback_models(cls) -> tuple[str, ...]:
        return _FALLBACK_GEMINI_MODELS

    def validate_model(self, model_id: str) -> bool:
        """Check if model exists in cache."""
        return model_id in self.models

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
