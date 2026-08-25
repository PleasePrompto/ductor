"""Persistent cache for Oh My Pi models with periodic refresh."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from ductor_bot.cli.model_cache import BaseModelCache
from ductor_bot.cli.omp_discovery import discover_omp_models
from ductor_bot.config import OMP_MODELS_ORDERED

_FALLBACK_OMP_MODELS: tuple[str, ...] = OMP_MODELS_ORDERED


@dataclass(frozen=True)
class OmpModelCache(BaseModelCache):
    """Immutable cache of Oh My Pi model selectors with refresh logic."""

    last_updated: str
    models: tuple[str, ...]

    @classmethod
    def _provider_name(cls) -> str:
        return "Omp"

    @classmethod
    async def _discover(cls) -> tuple[str, ...]:
        return await discover_omp_models()

    @classmethod
    def _empty_models(cls) -> tuple[str, ...]:
        return ()

    @classmethod
    def _fallback_models(cls) -> tuple[str, ...]:
        return _FALLBACK_OMP_MODELS

    def validate_model(self, model_id: str) -> bool:
        """Check if model exists in cache or is a known selector prefix."""
        if model_id in self.models:
            return True
        # Omp selectors are provider/model, e.g. anthropic/claude-opus-5
        # Accept fuzzy matches and plain model ids as valid.
        return "/" in model_id or model_id.startswith(("claude-", "gpt-", "gemini-", "openai/"))

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
