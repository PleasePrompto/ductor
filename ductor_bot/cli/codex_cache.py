"""Persistent cache for Codex models with periodic refresh."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ductor_bot.cli.codex_discovery import CodexModelInfo, discover_codex_models

logger = logging.getLogger(__name__)

_CACHE_MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class CodexModelCache:
    """Immutable cache of Codex models with refresh logic."""

    last_updated: str  # ISO 8601 timestamp
    models: list[CodexModelInfo]

    def get_model(self, model_id: str) -> CodexModelInfo | None:
        """Look up model by ID."""
        for model in self.models:
            if model.id == model_id:
                return model
        return None

    def validate_model(self, model_id: str) -> bool:
        """Check if model exists in cache."""
        return self.get_model(model_id) is not None

    def validate_reasoning_effort(self, model_id: str, effort: str) -> bool:
        """Check if effort is supported by model."""
        model = self.get_model(model_id)
        if model is None:
            return False
        if not model.supported_efforts:
            return False
        return effort in model.supported_efforts

    @classmethod
    async def load_or_refresh(
        cls,
        cache_path: Path,
        *,
        force_refresh: bool = False,
    ) -> CodexModelCache:
        """Load from disk, refresh if stale (>24h) or missing.

        Args:
            cache_path: Path to JSON cache file
            force_refresh: If True, ignore on-disk cache and rediscover models

        Returns:
            CodexModelCache (possibly refreshed)
        """
        if force_refresh:
            logger.info("Codex cache refresh forced")
            return await cls._refresh_and_save(cache_path)

        # Try to load from disk (use asyncio.to_thread for I/O)
        exists = await asyncio.to_thread(cache_path.exists)
        if exists:
            try:
                content = await asyncio.to_thread(cache_path.read_text)
                data = json.loads(content)
                cache = cls.from_json(data)

                # Check if stale
                last_updated = datetime.fromisoformat(cache.last_updated)
                age = datetime.now(UTC) - last_updated

                if age < _CACHE_MAX_AGE:
                    if cache.models:
                        logger.debug("Codex cache is fresh, using cached models")
                        return cache

                    logger.info("Codex cache is fresh but empty, forcing refresh")
                else:
                    logger.info("Codex cache is stale (age: %s), refreshing", age)
            except Exception:
                logger.warning("Failed to load Codex cache, will refresh", exc_info=True)

        # Refresh cache
        return await cls._refresh_and_save(cache_path)

    @classmethod
    async def _refresh_and_save(cls, cache_path: Path) -> CodexModelCache:
        """Discover models and save to disk."""
        try:
            models = await discover_codex_models()
            logger.info("Discovered %d Codex models", len(models))
        except Exception:
            logger.exception("Failed to discover Codex models, using empty cache")
            models = []

        cache = cls(
            last_updated=datetime.now(UTC).isoformat(),
            models=models,
        )

        # Save to disk (atomic write, async)
        try:
            await asyncio.to_thread(cache_path.parent.mkdir, parents=True, exist_ok=True)
            temp_path = cache_path.with_suffix(".tmp")
            content = json.dumps(cache.to_json(), indent=2)
            await asyncio.to_thread(temp_path.write_text, content)
            await asyncio.to_thread(temp_path.replace, cache_path)
            logger.debug("Saved Codex cache to %s", cache_path)
        except Exception:
            logger.exception("Failed to save Codex cache to disk")

        return cache

    def to_json(self) -> dict[str, Any]:
        """Serialize for persistence."""
        return {
            "last_updated": self.last_updated,
            "models": [
                {
                    "id": m.id,
                    "display_name": m.display_name,
                    "description": m.description,
                    "supported_efforts": list(m.supported_efforts),
                    "default_effort": m.default_effort,
                    "is_default": m.is_default,
                }
                for m in self.models
            ],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CodexModelCache:
        """Deserialize from JSON."""
        models = [
            CodexModelInfo(
                id=m["id"],
                display_name=m["display_name"],
                description=m["description"],
                supported_efforts=tuple(m["supported_efforts"]),
                default_effort=m["default_effort"],
                is_default=m["is_default"],
            )
            for m in data["models"]
        ]

        return cls(
            last_updated=data["last_updated"],
            models=models,
        )
