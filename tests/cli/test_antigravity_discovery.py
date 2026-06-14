"""Tests for dynamic Antigravity model discovery and caching."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.cli.antigravity_cache import _FALLBACK_ANTIGRAVITY_MODELS, AntigravityModelCache
from ductor_bot.cli.antigravity_discovery import _parse_models, discover_antigravity_models
from ductor_bot.cli.antigravity_runtime import (
    antigravity_process_env,
    configured_antigravity_models,
)
from ductor_bot.config import (
    ModelRegistry,
    reset_antigravity_models,
    set_antigravity_models,
)

_SAMPLE_OUTPUT = "Gemini 3.5 Flash (Medium)\nGemini 3.1 Pro (High)\nClaude Opus 4.6 (Thinking)\n"


@pytest.fixture(autouse=True)
def _reset_antigravity_models() -> Iterator[None]:
    reset_antigravity_models()
    yield
    reset_antigravity_models()


def test_parse_models_keeps_display_names() -> None:
    assert _parse_models(_SAMPLE_OUTPUT) == (
        "Gemini 3.5 Flash (Medium)",
        "Gemini 3.1 Pro (High)",
        "Claude Opus 4.6 (Thinking)",
    )


def test_parse_models_skips_blank_lines() -> None:
    assert _parse_models("\nGemini 3.5 Flash (Medium)\n\n") == ("Gemini 3.5 Flash (Medium)",)


def test_parse_models_rejects_usage_banner() -> None:
    assert _parse_models("Usage: agy models [flags]\n\nList available models") == ()


def _mock_proc(stdout: bytes, returncode: int = 0) -> AsyncMock:
    proc = AsyncMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode
    proc.pid = 4242
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


async def test_discover_returns_models_on_success() -> None:
    with (
        patch(
            "ductor_bot.cli.antigravity_discovery.asyncio.create_subprocess_exec",
            return_value=_mock_proc(_SAMPLE_OUTPUT.encode()),
        ) as create_process,
        patch(
            "ductor_bot.cli.antigravity_discovery.antigravity_process_env",
            return_value={"HOME": "host"},
        ),
    ):
        models = await discover_antigravity_models()

    assert models[0] == "Gemini 3.5 Flash (Medium)"
    assert len(models) == 3
    assert create_process.call_args.kwargs["env"] == {"HOME": "host"}


async def test_discover_returns_empty_on_nonzero_exit() -> None:
    with patch(
        "ductor_bot.cli.antigravity_discovery.asyncio.create_subprocess_exec",
        return_value=_mock_proc(b"boom", returncode=1),
    ):
        assert await discover_antigravity_models() == ()


async def test_discover_returns_empty_when_agy_missing() -> None:
    with patch(
        "ductor_bot.cli.antigravity_discovery.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError,
    ):
        assert await discover_antigravity_models() == ()


async def test_cache_falls_back_when_discovery_empty(tmp_path: Path) -> None:
    cache_path = tmp_path / "antigravity_models.json"
    with patch(
        "ductor_bot.cli.antigravity_cache.discover_antigravity_models",
        AsyncMock(return_value=()),
    ):
        cache = await AntigravityModelCache.load_or_refresh(cache_path, force_refresh=True)

    assert cache.models == _FALLBACK_ANTIGRAVITY_MODELS


async def test_cache_persists_models_from_official_settings(tmp_path: Path) -> None:
    cache_path = tmp_path / "antigravity_models.json"
    configured = "Future Antigravity Model"
    with (
        patch(
            "ductor_bot.cli.antigravity_cache.discover_antigravity_models",
            AsyncMock(return_value=()),
        ) as cli_discovery,
        patch(
            "ductor_bot.cli.antigravity_cache.configured_antigravity_models",
            return_value=(configured,),
        ),
    ):
        cache = await AntigravityModelCache.load_or_refresh(cache_path, force_refresh=True)

    assert cache.models == (*_FALLBACK_ANTIGRAVITY_MODELS, configured)
    assert cache_path.is_file()
    cli_discovery.assert_not_awaited()


def test_discovers_model_from_official_settings(tmp_path: Path) -> None:
    settings_dir = tmp_path / ".gemini" / "antigravity-cli"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text(
        '{"model": "Claude Opus 4.6 (Thinking)"}',
        encoding="utf-8",
    )

    assert configured_antigravity_models(tmp_path) == ("Claude Opus 4.6 (Thinking)",)


def test_process_env_removes_codex_sandbox_flag() -> None:
    env = antigravity_process_env(
        {
            "HOME": "host",
            "CODEX_SANDBOX_NETWORK_DISABLED": "1",
        }
    )

    assert env == {"HOME": "host"}


def test_runtime_display_name_routes_to_antigravity() -> None:
    set_antigravity_models(frozenset({"Claude Opus 4.6 (Thinking)"}))
    assert ModelRegistry().provider_for("Claude Opus 4.6 (Thinking)") == "antigravity"


def test_runtime_display_name_unknown_routes_to_codex() -> None:
    # Without discovery, a bare display name is not recognized as Antigravity.
    assert ModelRegistry().provider_for("Claude Opus 4.6 (Thinking)") == "codex"
