"""Tests for dynamic opencode model discovery and caching."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.cli.opencode_cache import OpencodeModelCache
from ductor_bot.cli.opencode_discovery import _parse_models, discover_opencode_models
from ductor_bot.config import (
    ModelRegistry,
    get_opencode_models_ordered,
    reset_opencode_models,
    set_opencode_models,
)

_SAMPLE_OUTPUT = """provider-a/model-one
provider-a/model-two
provider-b/model-three
"""


@pytest.fixture(autouse=True)
def _reset_opencode_models() -> Iterator[None]:
    reset_opencode_models()
    yield
    reset_opencode_models()


def test_parse_models_extracts_provider_model_ids() -> None:
    assert _parse_models(_SAMPLE_OUTPUT) == (
        "provider-a/model-one",
        "provider-a/model-two",
        "provider-b/model-three",
    )


def test_parse_models_skips_non_model_lines() -> None:
    raw = "Models:\nprovider-a/model-one\n\nUsage: opencode models\nList available models\n"
    assert _parse_models(raw) == ("provider-a/model-one",)


def test_parse_models_stops_at_usage_banner() -> None:
    assert _parse_models("Usage: opencode models\nList available models") == ()


def test_parse_models_dedupes() -> None:
    raw = "provider-a/model-one\nprovider-a/model-one\nprovider-a/model-two\n"
    assert _parse_models(raw) == ("provider-a/model-one", "provider-a/model-two")


def _mock_proc(stdout: bytes, returncode: int = 0) -> AsyncMock:
    proc = AsyncMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode
    proc.pid = 4242
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


async def test_discover_returns_models_on_success() -> None:
    with (
        patch("ductor_bot.cli.opencode_discovery.which", return_value="/usr/bin/opencode"),
        patch(
            "ductor_bot.cli.opencode_discovery.asyncio.create_subprocess_exec",
            return_value=_mock_proc(_SAMPLE_OUTPUT.encode()),
        ),
    ):
        models = await discover_opencode_models()

    assert models == (
        "provider-a/model-one",
        "provider-a/model-two",
        "provider-b/model-three",
    )


async def test_discover_returns_empty_when_not_authenticated() -> None:
    with (
        patch("ductor_bot.cli.opencode_discovery.which", return_value="/usr/bin/opencode"),
        patch(
            "ductor_bot.cli.opencode_discovery.asyncio.create_subprocess_exec",
            return_value=_mock_proc(
                b"No providers are configured. Run `opencode auth`.\n", returncode=1
            ),
        ),
    ):
        assert await discover_opencode_models() == ()


async def test_discover_returns_empty_when_binary_missing() -> None:
    with patch("ductor_bot.cli.opencode_discovery.which", return_value=None):
        assert await discover_opencode_models() == ()


async def test_cache_persists_discovered_models(tmp_path: Path) -> None:
    path = tmp_path / "opencode_models.json"
    with (
        patch(
            "ductor_bot.cli.opencode_cache.discover_opencode_models",
            return_value=("provider-a/model-one", "provider-a/model-two"),
        ),
        patch("ductor_bot.cli.opencode_cache.discover_opencode_default_model", return_value=""),
        patch("ductor_bot.cli.opencode_cache.discover_opencode_recent_models", return_value=()),
    ):
        cache = await OpencodeModelCache.load_or_refresh(path, force_refresh=True)
    assert cache.models == ("provider-a/model-one", "provider-a/model-two")
    assert path.is_file()
    loaded = OpencodeModelCache.from_json(json.loads(path.read_text()))
    assert loaded.models == cache.models


async def test_cache_load_attaches_default_and_recent(tmp_path: Path) -> None:
    """load_or_refresh always re-discovers default/recent, even from disk cache."""
    from datetime import UTC, datetime, timedelta

    path = tmp_path / "opencode_models.json"
    path.write_text(
        json.dumps(
            {
                "last_updated": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                "models": ["provider-a/model-one"],
                "default_model": "",
                "recent_models": [],
            }
        )
    )
    with (
        patch("ductor_bot.cli.opencode_cache.discover_opencode_default_model", return_value=""),
        patch(
            "ductor_bot.cli.opencode_cache.discover_opencode_recent_models",
            return_value=("provider-a/model-recent",),
        ),
    ):
        cache = await OpencodeModelCache.load_or_refresh(path, force_refresh=False)
    assert cache.models == ("provider-a/model-one",)
    assert cache.recent_models == ("provider-a/model-recent",)
    # No explicit config default -> most recently used model becomes the default.
    assert cache.default_model == "provider-a/model-recent"


def test_cache_json_round_trip_with_extras() -> None:
    cache = OpencodeModelCache(
        last_updated="2026-01-01T00:00:00+00:00",
        models=("provider-a/model-one",),
        default_model="provider-a/model-recent",
        recent_models=("provider-a/model-recent", "provider-b/model-two"),
    )
    restored = OpencodeModelCache.from_json(cache.to_json())
    assert restored == cache


def test_normalize_session_model() -> None:
    from ductor_bot.cli.opencode_discovery import _normalize_session_model

    assert (
        _normalize_session_model(
            '{"id":"model-recent","providerID":"provider-a","variant":"default"}'
        )
        == "provider-a/model-recent"
    )
    # Variants (reasoning effort) are not part of the model ID.
    assert (
        _normalize_session_model('{"id":"model-recent","providerID":"provider-a"}')
        == "provider-a/model-recent"
    )
    assert _normalize_session_model({"id": "model-two", "providerID": "provider-a"}) == (
        "provider-a/model-two"
    )
    # Old plain-string shape without a provider is unusable for --model.
    assert _normalize_session_model("model-recent") == ""
    assert _normalize_session_model("") == ""
    assert _normalize_session_model(None) == ""


def _make_session_db(tmp_path: Path, rows: list[tuple[str, int]]) -> None:
    import sqlite3

    db_dir = tmp_path / "opencode"
    db_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_dir / "opencode.db")
    con.execute("CREATE TABLE session (model TEXT, time_updated INTEGER)")
    con.executemany("INSERT INTO session (model, time_updated) VALUES (?, ?)", rows)
    con.commit()
    con.close()


def test_read_recent_models_order_and_dedupe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ductor_bot.cli.opencode_discovery import _read_recent_models

    _make_session_db(
        tmp_path,
        [
            ('{"id":"model-recent","providerID":"provider-a"}', 100),
            ('{"id":"model-recent","providerID":"provider-a","variant":"low"}', 300),
            ('{"id":"model-two","providerID":"provider-a"}', 200),
            ("bare-id-without-provider", 400),
        ],
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert _read_recent_models(10) == (
        "provider-a/model-recent",  # newest variant wins the slot
        "provider-a/model-two",
    )
    assert _read_recent_models(1) == ("provider-a/model-recent",)


def test_read_recent_models_missing_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ductor_bot.cli.opencode_discovery import _read_recent_models

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "absent"))
    assert _read_recent_models(10) == ()


def test_read_default_model_from_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ductor_bot.cli.opencode_discovery import _read_default_model

    config_dir = tmp_path / "opencode"
    config_dir.mkdir(parents=True)
    (config_dir / "opencode.json").write_text(
        '{"model": "provider-a/model-recent", "theme": "dark"}'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert _read_default_model() == "provider-a/model-recent"


def test_read_default_model_from_jsonc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ductor_bot.cli.opencode_discovery import _read_default_model

    config_dir = tmp_path / "opencode"
    config_dir.mkdir(parents=True)
    (config_dir / "opencode.jsonc").write_text(
        '{\n  "$schema": "https://opencode.ai/config.json",\n'
        '  "model": "provider-a/model-one", // comment\n}\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert _read_default_model() == "provider-a/model-one"


def test_read_default_model_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ductor_bot.cli.opencode_discovery import _read_default_model

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "absent"))
    assert _read_default_model() == ""


def test_validate_model_accepts_provider_model_ids() -> None:
    cache = OpencodeModelCache(last_updated="2026-01-01T00:00:00+00:00", models=())
    # opencode can run any provider/model its credentials support.
    assert cache.validate_model("provider-a/model-one") is True
    assert cache.validate_model("provider-b/model-two") is True
    # Bare model IDs are rejected (--model requires the qualified form).
    assert cache.validate_model("model-one") is False


def test_set_opencode_models_updates_registry_and_order() -> None:
    set_opencode_models(("provider-a/model-one", "provider-a/model-two"))
    assert get_opencode_models_ordered() == (
        "provider-a/model-one",
        "provider-a/model-two",
    )
    assert ModelRegistry.provider_for("provider-a/model-one") == "opencode"
    assert ModelRegistry.provider_for("provider-a/model-two") == "opencode"


def test_observer_pushes_default_and_recent_into_runtime(tmp_path: Path) -> None:
    from ductor_bot.cli.opencode_cache_observer import OpencodeCacheObserver
    from ductor_bot.config import (
        get_opencode_default_model,
        get_opencode_recent_models,
        reset_opencode_models,
    )

    reset_opencode_models()
    cache = OpencodeModelCache(
        last_updated="2026-01-01T00:00:00+00:00",
        models=("provider-a/model-two",),
        default_model="provider-a/model-recent",
        recent_models=("provider-a/model-recent", "provider-a/model-two"),
    )
    observer = OpencodeCacheObserver(tmp_path / "opencode_models.json")
    observer._cache = cache
    try:
        observer._on_cache_loaded()
        assert get_opencode_default_model() == "provider-a/model-recent"
        assert get_opencode_recent_models() == (
            "provider-a/model-recent",
            "provider-a/model-two",
        )
    finally:
        reset_opencode_models()
