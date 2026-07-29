"""Persistent cache for Grok Build models with per-model effort menus."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from ductor_bot.cli.grok_discovery import GrokModelInfo, discover_grok_models, order_efforts
from ductor_bot.cli.model_cache import BaseModelCache
from ductor_bot.config import GROK_MODELS_ORDERED, GROK_SUPPORTED_EFFORTS

# Hardcoded fallback when discovery and disk cache both fail.
_FALLBACK_GROK_MODELS: list[GrokModelInfo] = [
    GrokModelInfo(
        id=model_id,
        supported_efforts=GROK_SUPPORTED_EFFORTS,
        default_effort="medium" if "medium" in GROK_SUPPORTED_EFFORTS else GROK_SUPPORTED_EFFORTS[0],
    )
    for model_id in GROK_MODELS_ORDERED
]


@dataclass(frozen=True)
class GrokModelCache(BaseModelCache):
    """Immutable cache of Grok Build models (IDs + effort menus) with refresh logic."""

    last_updated: str  # ISO 8601 timestamp
    models: list[GrokModelInfo]

    @classmethod
    def _provider_name(cls) -> str:
        return "Grok"

    @classmethod
    async def _discover(cls) -> list[GrokModelInfo]:
        return await discover_grok_models()

    @classmethod
    def _empty_models(cls) -> list[GrokModelInfo]:
        return []

    @classmethod
    def _fallback_models(cls) -> list[GrokModelInfo]:
        return list(_FALLBACK_GROK_MODELS)

    def model_ids(self) -> tuple[str, ...]:
        """Ordered model IDs (for registry / /model list)."""
        return tuple(m.id for m in self.models)

    def get_model(self, model_id: str) -> GrokModelInfo | None:
        """Look up model by ID."""
        for model in self.models:
            if model.id == model_id:
                return model
        return None

    def validate_model(self, model_id: str) -> bool:
        """Check if model exists in cache (or is a grok-* ID)."""
        return self.get_model(model_id) is not None or model_id.startswith("grok-")

    def validate_reasoning_effort(self, model_id: str, effort: str) -> bool:
        """Check if effort is supported by the model (or conservative fallback)."""
        model = self.get_model(model_id)
        if model is not None:
            return bool(model.supported_efforts) and effort in model.supported_efforts
        # Unknown grok-* ID: allow only the conservative fallback set.
        return effort in GROK_SUPPORTED_EFFORTS

    def to_json(self) -> dict[str, Any]:
        """Serialize for persistence."""
        return {
            "last_updated": self.last_updated,
            "models": [
                {
                    "id": m.id,
                    "supported_efforts": list(m.supported_efforts),
                    "default_effort": m.default_effort,
                }
                for m in self.models
            ],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        """Deserialize from JSON (supports legacy ID-only caches)."""
        models: list[GrokModelInfo] = []
        for entry in data.get("models", []):
            if isinstance(entry, str):
                # Legacy format: bare model ID strings without effort menus.
                models.append(
                    GrokModelInfo(
                        id=entry,
                        supported_efforts=GROK_SUPPORTED_EFFORTS,
                        default_effort="medium",
                    )
                )
                continue
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("id", "")).strip()
            if not model_id:
                continue
            raw_efforts = entry.get("supported_efforts") or list(GROK_SUPPORTED_EFFORTS)
            efforts = order_efforts(tuple(str(e) for e in raw_efforts))
            if not efforts:
                efforts = GROK_SUPPORTED_EFFORTS
            default = str(entry.get("default_effort") or "")
            if default not in efforts:
                default = "medium" if "medium" in efforts else efforts[0]
            models.append(
                GrokModelInfo(
                    id=model_id,
                    supported_efforts=efforts,
                    default_effort=default,
                )
            )

        return cls(
            last_updated=str(data.get("last_updated", "")),
            models=models,
        )
