"""Tests for dynamic Grok Build model discovery and caching."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.cli.grok_cache import _FALLBACK_GROK_MODELS, GrokModelCache
from ductor_bot.cli.grok_discovery import _parse_models, discover_grok_models
from ductor_bot.config import (
    ModelRegistry,
    get_grok_models_ordered,
    reset_grok_models,
    set_grok_models,
)

_SAMPLE_OUTPUT = """You are logged in with grok.com.

Default model: grok-4.5

Available models:
  * grok-4.5 (default)
  - grok-composer-2.5-fast
"""


@pytest.fixture(autouse=True)
def _reset_grok_models() -> Iterator[None]:
    reset_grok_models()
    yield
    reset_grok_models()


def test_parse_models_bullets_and_default_suffix() -> None:
    assert _parse_models(_SAMPLE_OUTPUT) == (
        "grok-4.5",
        "grok-composer-2.5-fast",
    )


def test_parse_models_default_only_fallback() -> None:
    assert _parse_models("Default model: grok-4.5\n") == ("grok-4.5",)


def test_parse_models_rejects_usage_banner() -> None:
    assert _parse_models("Usage: grok models\nList available models") == ()


def test_parse_models_skips_duplicates() -> None:
    raw = "  * grok-4.5\n  - grok-4.5\n  - grok-new\n"
    assert _parse_models(raw) == ("grok-4.5", "grok-new")


def _mock_proc(stdout: bytes, returncode: int = 0) -> AsyncMock:
    proc = AsyncMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode
    proc.pid = 4242
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


async def test_discover_returns_models_on_success() -> None:
    with (
        patch("ductor_bot.cli.grok_discovery.which", return_value="/usr/bin/grok"),
        patch(
            "ductor_bot.cli.grok_discovery.asyncio.create_subprocess_exec",
            return_value=_mock_proc(_SAMPLE_OUTPUT.encode()),
        ),
    ):
        models = await discover_grok_models()

    assert models == ("grok-4.5", "grok-composer-2.5-fast")


async def test_discover_returns_empty_when_not_logged_in() -> None:
    with (
        patch("ductor_bot.cli.grok_discovery.which", return_value="/usr/bin/grok"),
        patch(
            "ductor_bot.cli.grok_discovery.asyncio.create_subprocess_exec",
            return_value=_mock_proc(b"not logged in\nrun `grok login`\n", returncode=1),
        ),
    ):
        assert await discover_grok_models() == ()


async def test_discover_returns_empty_when_binary_missing() -> None:
    with patch("ductor_bot.cli.grok_discovery.which", return_value=None):
        assert await discover_grok_models() == ()


async def test_cache_persists_discovered_models(tmp_path: Path) -> None:
    path = tmp_path / "grok_models.json"
    with patch(
        "ductor_bot.cli.grok_cache.discover_grok_models",
        return_value=("grok-4.5", "grok-future"),
    ):
        cache = await GrokModelCache.load_or_refresh(path, force_refresh=True)
    assert cache.models == ("grok-4.5", "grok-future")
    assert path.is_file()
    loaded = GrokModelCache.from_json(json.loads(path.read_text()))
    assert loaded.models == cache.models


def test_set_grok_models_updates_registry_and_order() -> None:
    set_grok_models(("grok-z", "grok-a"))
    assert get_grok_models_ordered() == ("grok-z", "grok-a")
    assert ModelRegistry.provider_for("grok-z") == "grok"
    assert ModelRegistry.provider_for("grok-a") == "grok"


def test_fallback_models_match_hardcoded() -> None:
    assert "grok-4.5" in _FALLBACK_GROK_MODELS
