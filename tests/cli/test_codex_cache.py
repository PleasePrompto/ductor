"""Tests for Codex model cache."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_discovery import CodexModelInfo


@pytest.fixture
def sample_models() -> list[CodexModelInfo]:
    """Sample model list for testing."""
    return [
        CodexModelInfo(
            id="gpt-4o",
            display_name="GPT-4o",
            description="GPT-4o model",
            supported_efforts=("low", "medium", "high"),
            default_effort="medium",
            is_default=True,
        ),
        CodexModelInfo(
            id="gpt-4o-mini",
            display_name="GPT-4o Mini",
            description="GPT-4o Mini model (no reasoning)",
            supported_efforts=(),
            default_effort="",
            is_default=False,
        ),
    ]


@pytest.fixture
def fresh_cache(sample_models: list[CodexModelInfo]) -> CodexModelCache:
    """Fresh cache (< 24h old)."""
    return CodexModelCache(
        last_updated=datetime.now(UTC).isoformat(),
        models=sample_models,
    )


@pytest.fixture
def stale_cache(sample_models: list[CodexModelInfo]) -> CodexModelCache:
    """Stale cache (> 24h old)."""
    old_time = datetime.now(UTC) - timedelta(hours=25)
    return CodexModelCache(
        last_updated=old_time.isoformat(),
        models=sample_models,
    )


async def test_load_from_disk(tmp_path: Path) -> None:
    """Should load cache from disk if present and fresh."""
    cache_path = tmp_path / "codex_models.json"
    now = datetime.now(UTC).isoformat()
    cache_path.write_text(
        f"""{{
        "last_updated": "{now}",
        "models": [
            {{
                "id": "gpt-4o",
                "display_name": "GPT-4o",
                "description": "GPT-4o model",
                "supported_efforts": ["low", "medium", "high"],
                "default_effort": "medium",
                "is_default": true
            }}
        ]
    }}"""
    )

    with patch("ductor_bot.cli.codex_cache.discover_codex_models", AsyncMock()) as mock_discover:
        result = await CodexModelCache.load_or_refresh(cache_path)

        assert len(result.models) == 1
        assert result.models[0].id == "gpt-4o"
        mock_discover.assert_not_called()  # Should not refresh if fresh


async def test_refresh_on_stale(tmp_path: Path, sample_models: list[CodexModelInfo]) -> None:
    """Should refresh cache if stale (>24h)."""
    cache_path = tmp_path / "codex_models.json"
    old_time = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    cache_path.write_text(
        f"""{{
        "last_updated": "{old_time}",
        "models": []
    }}"""
    )

    with patch(
        "ductor_bot.cli.codex_cache.discover_codex_models",
        AsyncMock(return_value=sample_models),
    ) as mock_discover:
        result = await CodexModelCache.load_or_refresh(cache_path)

        mock_discover.assert_called_once()
        assert len(result.models) == 2
        assert result.models[0].id == "gpt-4o"

        # Should write updated cache to disk
        assert cache_path.exists()


async def test_skip_refresh_if_recent(tmp_path: Path) -> None:
    """Should skip refresh if cache is recent (<24h)."""
    cache_path = tmp_path / "codex_models.json"
    recent_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    cache_path.write_text(
        f"""{{
        "last_updated": "{recent_time}",
        "models": [
            {{
                "id": "gpt-4o",
                "display_name": "GPT-4o",
                "description": "GPT-4o model",
                "supported_efforts": ["low"],
                "default_effort": "low",
                "is_default": true
            }}
        ]
    }}"""
    )

    with patch("ductor_bot.cli.codex_cache.discover_codex_models", AsyncMock()) as mock_discover:
        result = await CodexModelCache.load_or_refresh(cache_path)

        mock_discover.assert_not_called()
        assert len(result.models) == 1


async def test_refresh_if_recent_but_empty(
    tmp_path: Path,
    sample_models: list[CodexModelInfo],
) -> None:
    """Should refresh if cache is recent but contains zero models."""
    cache_path = tmp_path / "codex_models.json"
    recent_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    cache_path.write_text(
        f"""{{
        "last_updated": "{recent_time}",
        "models": []
    }}"""
    )

    with patch(
        "ductor_bot.cli.codex_cache.discover_codex_models",
        AsyncMock(return_value=sample_models),
    ) as mock_discover:
        result = await CodexModelCache.load_or_refresh(cache_path)

        mock_discover.assert_called_once()
        assert len(result.models) == 2


async def test_force_refresh_ignores_fresh_cache(
    tmp_path: Path,
    sample_models: list[CodexModelInfo],
) -> None:
    """Should refresh when force_refresh=True even if cache is fresh."""
    cache_path = tmp_path / "codex_models.json"
    recent_time = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    cache_path.write_text(
        f"""{{
        "last_updated": "{recent_time}",
        "models": [
            {{
                "id": "stale-model",
                "display_name": "stale-model",
                "description": "old",
                "supported_efforts": ["low"],
                "default_effort": "low",
                "is_default": true
            }}
        ]
    }}"""
    )

    with patch(
        "ductor_bot.cli.codex_cache.discover_codex_models",
        AsyncMock(return_value=sample_models),
    ) as mock_discover:
        result = await CodexModelCache.load_or_refresh(cache_path, force_refresh=True)

        mock_discover.assert_called_once()
        assert len(result.models) == 2
        assert result.models[0].id == "gpt-4o"


def test_validate_model_exists(fresh_cache: CodexModelCache) -> None:
    """Should return True for existing model."""
    assert fresh_cache.validate_model("gpt-4o") is True
    assert fresh_cache.validate_model("gpt-4o-mini") is True


def test_validate_model_missing(fresh_cache: CodexModelCache) -> None:
    """Should return False for nonexistent model."""
    assert fresh_cache.validate_model("nonexistent") is False


def test_validate_reasoning_effort(fresh_cache: CodexModelCache) -> None:
    """Should validate reasoning effort against model capabilities."""
    assert fresh_cache.validate_reasoning_effort("gpt-4o", "low") is True
    assert fresh_cache.validate_reasoning_effort("gpt-4o", "medium") is True
    assert fresh_cache.validate_reasoning_effort("gpt-4o", "high") is True


def test_validate_reasoning_effort_invalid(fresh_cache: CodexModelCache) -> None:
    """Should return False for invalid or unsupported effort."""
    # Model doesn't support reasoning
    assert fresh_cache.validate_reasoning_effort("gpt-4o-mini", "low") is False

    # Invalid effort for model that supports reasoning
    assert fresh_cache.validate_reasoning_effort("gpt-4o", "extreme") is False

    # Nonexistent model
    assert fresh_cache.validate_reasoning_effort("nonexistent", "low") is False


async def test_cache_empty_on_discovery_failure(tmp_path: Path) -> None:
    """Should create empty cache if discovery fails."""
    cache_path = tmp_path / "codex_models.json"

    with patch(
        "ductor_bot.cli.codex_cache.discover_codex_models",
        AsyncMock(side_effect=Exception("Discovery failed")),
    ):
        result = await CodexModelCache.load_or_refresh(cache_path)

        assert len(result.models) == 0


def test_serialize_deserialize(fresh_cache: CodexModelCache) -> None:
    """Should roundtrip serialize and deserialize."""
    json_data = fresh_cache.to_json()

    assert "last_updated" in json_data
    assert "models" in json_data
    assert len(json_data["models"]) == 2  # type: ignore[arg-type]

    restored = CodexModelCache.from_json(json_data)

    assert restored.last_updated == fresh_cache.last_updated
    assert len(restored.models) == len(fresh_cache.models)
    assert restored.models[0].id == fresh_cache.models[0].id
    assert restored.models[1].supported_efforts == fresh_cache.models[1].supported_efforts
