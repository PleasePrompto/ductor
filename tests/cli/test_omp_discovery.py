"""Tests for Oh My Pi model discovery and caching."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.cli.omp_cache import OmpModelCache
from ductor_bot.cli.omp_discovery import _parse_models, discover_omp_models
from ductor_bot.config import (
    ModelRegistry,
    get_omp_models_ordered,
    reset_omp_models,
    set_omp_models,
)

_SAMPLE_JSON = json.dumps(
    {
        "models": [
            {
                "provider": "anthropic",
                "id": "claude-opus-5",
                "selector": "anthropic/claude-opus-5",
                "name": "Claude Opus 5",
            },
            {
                "provider": "openai",
                "id": "gpt-5",
                "selector": "openai/gpt-5",
                "name": "GPT 5",
            },
        ]
    }
)


@pytest.fixture(autouse=True)
def _reset_omp_models() -> Iterator[None]:
    reset_omp_models()
    yield
    reset_omp_models()


def test_parse_models_selectors() -> None:
    assert _parse_models(_SAMPLE_JSON) == (
        "anthropic/claude-opus-5",
        "openai/gpt-5",
    )


def test_parse_models_empty_on_invalid_json() -> None:
    assert _parse_models("not json") == ()


def test_parse_models_empty_on_missing_key() -> None:
    assert _parse_models(json.dumps({"foo": []})) == ()


def test_parse_models_skips_duplicates() -> None:
    raw = json.dumps(
        {
            "models": [
                {"selector": "anthropic/claude-opus-5"},
                {"selector": "anthropic/claude-opus-5"},
                {"selector": "openai/gpt-5"},
            ]
        }
    )
    assert _parse_models(raw) == ("anthropic/claude-opus-5", "openai/gpt-5")


def _mock_proc(stdout: bytes, returncode: int = 0) -> AsyncMock:
    proc = AsyncMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode
    proc.pid = 4242
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


async def test_discover_returns_models_on_success() -> None:
    with (
        patch("ductor_bot.cli.omp_discovery.which", return_value="/usr/bin/omp"),
        patch(
            "ductor_bot.cli.omp_discovery.asyncio.create_subprocess_exec",
            return_value=_mock_proc(_SAMPLE_JSON.encode()),
        ),
    ):
        models = await discover_omp_models()

    assert models == ("anthropic/claude-opus-5", "openai/gpt-5")


async def test_discover_returns_empty_when_binary_missing() -> None:
    with patch("ductor_bot.cli.omp_discovery.which", return_value=None):
        assert await discover_omp_models() == ()


async def test_discover_returns_empty_on_nonzero_exit() -> None:
    with (
        patch("ductor_bot.cli.omp_discovery.which", return_value="/usr/bin/omp"),
        patch(
            "ductor_bot.cli.omp_discovery.asyncio.create_subprocess_exec",
            return_value=_mock_proc(b"error", returncode=1),
        ),
    ):
        assert await discover_omp_models() == ()


async def test_cache_persists_discovered_models(tmp_path: Path) -> None:
    path = tmp_path / "omp_models.json"
    with patch(
        "ductor_bot.cli.omp_cache.discover_omp_models",
        return_value=("anthropic/claude-opus-5", "openai/gpt-5"),
    ):
        cache = await OmpModelCache.load_or_refresh(path, force_refresh=True)
    assert cache.models == ("anthropic/claude-opus-5", "openai/gpt-5")
    assert path.is_file()
    loaded = OmpModelCache.from_json(json.loads(path.read_text()))
    assert loaded.models == cache.models


def test_set_omp_models_updates_registry_and_order() -> None:
    set_omp_models(("openai/gpt-5", "anthropic/claude-opus-5"))
    assert get_omp_models_ordered() == ("openai/gpt-5", "anthropic/claude-opus-5")
    assert ModelRegistry.provider_for("openai/gpt-5") == "omp"
    assert ModelRegistry.provider_for("anthropic/claude-opus-5") == "omp"
