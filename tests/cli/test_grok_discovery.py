"""Tests for dynamic Grok Build model discovery, effort probing, and caching."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.cli.grok_cache import _FALLBACK_GROK_MODELS, GrokModelCache
from ductor_bot.cli.grok_discovery import (
    GrokModelInfo,
    _parse_models,
    _parse_supported_efforts,
    discover_grok_models,
    order_efforts,
)
from ductor_bot.config import (
    ModelRegistry,
    get_grok_models_ordered,
    get_grok_supported_efforts,
    reset_grok_models,
    set_grok_model_efforts,
    set_grok_models,
)

_SAMPLE_OUTPUT = """You are logged in with grok.com.

Default model: grok-4.5

Available models:
  * grok-4.5 (default)
  - grok-composer-2.5-fast
"""

_PROBE_ERROR = (
    '{"type":"error","message":"--effort/--reasoning-effort: unknown effort level '
    "'__ductor_probe__'; use one of: high, medium, low\"}\n"
    "Error: --effort/--reasoning-effort: unknown effort level '__ductor_probe__'; "
    "use one of: high, medium, low\n"
)


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


def test_parse_supported_efforts_from_cli_error() -> None:
    assert _parse_supported_efforts(_PROBE_ERROR) == ("low", "medium", "high")


def test_parse_supported_efforts_orders_canonical_levels() -> None:
    assert order_efforts(("xhigh", "none", "medium", "low")) == (
        "none",
        "low",
        "medium",
        "xhigh",
    )


def test_parse_supported_efforts_empty_when_no_menu() -> None:
    assert _parse_supported_efforts("some unrelated failure") == ()


def _mock_proc(stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode
    proc.pid = 4242
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


async def test_discover_returns_models_with_probed_efforts() -> None:
    calls: list[tuple[str, ...]] = []

    async def _spawn(*args: object, **_kwargs: object) -> AsyncMock:
        cmd = tuple(str(a) for a in args)
        calls.append(cmd)
        if len(args) >= 2 and args[1] == "models":
            return _mock_proc(_SAMPLE_OUTPUT.encode())
        # Effort probe: argv contains --model <id>
        return _mock_proc(b"", returncode=1, stderr=_PROBE_ERROR.encode())

    with (
        patch("ductor_bot.cli.grok_discovery.which", return_value="/usr/bin/grok"),
        patch(
            "ductor_bot.cli.grok_discovery.asyncio.create_subprocess_exec",
            side_effect=_spawn,
        ),
    ):
        models = await discover_grok_models()

    assert [m.id for m in models] == ["grok-4.5", "grok-composer-2.5-fast"]
    assert all(m.supported_efforts == ("low", "medium", "high") for m in models)
    assert all(m.default_effort == "medium" for m in models)
    # models list + one probe per model
    assert sum(1 for c in calls if len(c) >= 2 and c[1] == "models") == 1
    assert sum(1 for c in calls if "--reasoning-effort" in c) == 2


async def test_discover_returns_empty_when_not_logged_in() -> None:
    with (
        patch("ductor_bot.cli.grok_discovery.which", return_value="/usr/bin/grok"),
        patch(
            "ductor_bot.cli.grok_discovery.asyncio.create_subprocess_exec",
            return_value=_mock_proc(b"not logged in\nrun `grok login`\n", returncode=1),
        ),
    ):
        assert await discover_grok_models() == []


async def test_discover_returns_empty_when_binary_missing() -> None:
    with patch("ductor_bot.cli.grok_discovery.which", return_value=None):
        assert await discover_grok_models() == []


async def test_discover_uses_fallback_efforts_when_probe_unparseable() -> None:
    async def _spawn(*args: object, **_kwargs: object) -> AsyncMock:
        if len(args) >= 2 and args[1] == "models":
            return _mock_proc(b"  * grok-4.5\n")
        return _mock_proc(b"boom", returncode=1)

    with (
        patch("ductor_bot.cli.grok_discovery.which", return_value="/usr/bin/grok"),
        patch(
            "ductor_bot.cli.grok_discovery.asyncio.create_subprocess_exec",
            side_effect=_spawn,
        ),
    ):
        models = await discover_grok_models()

    assert len(models) == 1
    assert models[0].id == "grok-4.5"
    assert models[0].supported_efforts == ("low", "medium", "high")


async def test_cache_persists_discovered_models_and_efforts(tmp_path: Path) -> None:
    path = tmp_path / "grok_models.json"
    discovered = [
        GrokModelInfo(
            id="grok-4.5",
            supported_efforts=("low", "medium", "high"),
            default_effort="medium",
        ),
        GrokModelInfo(
            id="grok-future",
            supported_efforts=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
            default_effort="medium",
        ),
    ]
    with patch(
        "ductor_bot.cli.grok_cache.discover_grok_models",
        return_value=discovered,
    ):
        cache = await GrokModelCache.load_or_refresh(path, force_refresh=True)
    assert [m.id for m in cache.models] == ["grok-4.5", "grok-future"]
    assert cache.get_model("grok-4.5") is not None
    assert cache.get_model("grok-4.5").supported_efforts == ("low", "medium", "high")  # type: ignore[union-attr]
    assert cache.validate_reasoning_effort("grok-4.5", "minimal") is False
    assert cache.validate_reasoning_effort("grok-4.5", "low") is True
    assert path.is_file()
    loaded = GrokModelCache.from_json(json.loads(path.read_text()))
    assert [m.id for m in loaded.models] == [m.id for m in cache.models]
    assert loaded.get_model("grok-future").supported_efforts == (  # type: ignore[union-attr]
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )


def test_cache_from_json_accepts_legacy_id_only_format() -> None:
    cache = GrokModelCache.from_json(
        {
            "last_updated": "2026-07-29T00:00:00+00:00",
            "models": ["grok-4.5", "grok-composer-2.5-fast"],
        }
    )
    assert [m.id for m in cache.models] == ["grok-4.5", "grok-composer-2.5-fast"]
    assert cache.get_model("grok-4.5").supported_efforts == ("low", "medium", "high")  # type: ignore[union-attr]


def test_set_grok_models_updates_registry_and_order() -> None:
    set_grok_models(("grok-z", "grok-a"))
    assert get_grok_models_ordered() == ("grok-z", "grok-a")
    assert ModelRegistry.provider_for("grok-z") == "grok"
    assert ModelRegistry.provider_for("grok-a") == "grok"


def test_set_grok_model_efforts_drives_lookup() -> None:
    set_grok_models(("grok-4.5",))
    set_grok_model_efforts({"grok-4.5": ("low", "medium", "high")})
    assert get_grok_supported_efforts("grok-4.5") == ("low", "medium", "high")
    # Unknown model falls back to conservative constant.
    assert get_grok_supported_efforts("grok-unknown") == ("low", "medium", "high")


def test_fallback_models_include_grok_45() -> None:
    assert any(m.id == "grok-4.5" for m in _FALLBACK_GROK_MODELS)
    assert all(m.supported_efforts for m in _FALLBACK_GROK_MODELS)
