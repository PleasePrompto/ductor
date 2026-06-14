"""Persistent cache for Antigravity models with periodic refresh."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from ductor_bot.cli.antigravity_discovery import discover_antigravity_models
from ductor_bot.cli.antigravity_runtime import configured_antigravity_models
from ductor_bot.cli.model_cache import BaseModelCache

# Hardcoded fallback when discovery and disk cache both fail. Mirrors the
# models exposed by ``agy models``; the next successful discovery replaces it.
_FALLBACK_ANTIGRAVITY_MODELS: tuple[str, ...] = (
    "Gemini 3.5 Flash (Medium)",
    "Gemini 3.5 Flash (High)",
    "Gemini 3.5 Flash (Low)",
    "Gemini 3.1 Pro (Low)",
    "Gemini 3.1 Pro (High)",
    "Claude Sonnet 4.6 (Thinking)",
    "Claude Opus 4.6 (Thinking)",
    "GPT-OSS 120B (Medium)",
)


@dataclass(frozen=True)
class AntigravityModelCache(BaseModelCache):
    """Immutable cache of Antigravity model display names with refresh logic."""

    last_updated: str  # ISO 8601 timestamp
    models: tuple[str, ...]

    @classmethod
    def _provider_name(cls) -> str:
        return "Antigravity"

    @classmethod
    async def _discover(cls) -> tuple[str, ...]:
        configured = configured_antigravity_models()
        if configured:
            # agy 1.0.x can hang or return empty stdout outside a TTY. Official
            # settings prove the CLI is configured and identify the selected
            # model, so avoid a redundant network probe for every agent home.
            return tuple(dict.fromkeys((*_FALLBACK_ANTIGRAVITY_MODELS, *configured)))

        return await discover_antigravity_models()

    @classmethod
    def _empty_models(cls) -> tuple[str, ...]:
        return ()

    @classmethod
    def _fallback_models(cls) -> tuple[str, ...]:
        return _FALLBACK_ANTIGRAVITY_MODELS

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
