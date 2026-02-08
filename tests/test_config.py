"""Tests for config and model registry."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ductor_bot.config import (
    AgentConfig,
    DockerConfig,
    ModelRegistry,
    StreamingConfig,
    deep_merge_config,
)

# -- AgentConfig defaults --


def test_agent_config_defaults() -> None:
    cfg = AgentConfig()
    assert cfg.provider == "claude"
    assert cfg.model == "opus"
    assert cfg.idle_timeout_minutes == 1440
    assert cfg.daily_reset_hour == 4
    assert cfg.cli_timeout == 600.0
    assert cfg.permission_mode == "bypassPermissions"
    assert cfg.telegram_token == ""
    assert cfg.allowed_user_ids == []


def test_agent_config_streaming_defaults() -> None:
    cfg = AgentConfig()
    assert cfg.streaming.enabled is True
    assert cfg.streaming.min_chars == 200
    assert cfg.streaming.max_chars == 4000


def test_agent_config_docker_defaults() -> None:
    cfg = AgentConfig()
    assert cfg.docker.enabled is False
    assert cfg.docker.image_name == "ductor-sandbox"


def test_agent_config_rejects_invalid_types() -> None:
    with pytest.raises(ValidationError, match="idle_timeout_minutes"):
        AgentConfig(idle_timeout_minutes="not_a_number")  # type: ignore[arg-type]


# -- deep_merge_config --


def test_deep_merge_adds_new_keys() -> None:
    user: dict[str, object] = {"model": "sonnet"}
    defaults: dict[str, object] = {"model": "opus", "provider": "claude"}
    merged, changed = deep_merge_config(user, defaults)
    assert merged["model"] == "sonnet"
    assert merged["provider"] == "claude"
    assert changed is True


def test_deep_merge_preserves_user_values() -> None:
    user: dict[str, object] = {"model": "sonnet", "provider": "codex"}
    defaults: dict[str, object] = {"model": "opus", "provider": "claude"}
    merged, changed = deep_merge_config(user, defaults)
    assert merged["model"] == "sonnet"
    assert merged["provider"] == "codex"
    assert changed is False


def test_deep_merge_nested() -> None:
    user: dict[str, object] = {"streaming": {"enabled": False}}
    defaults: dict[str, object] = {"streaming": {"enabled": True, "min_chars": 200}}
    merged, changed = deep_merge_config(user, defaults)
    streaming = merged["streaming"]
    assert isinstance(streaming, dict)
    assert streaming["enabled"] is False
    assert streaming["min_chars"] == 200
    assert changed is True


def test_deep_merge_no_change() -> None:
    data: dict[str, object] = {"a": 1, "b": 2}
    defaults: dict[str, object] = {"a": 99, "b": 99}
    _, changed = deep_merge_config(data, defaults)
    assert changed is False


# -- ModelRegistry --


def test_registry_provider_for_claude() -> None:
    reg = ModelRegistry()
    assert reg.provider_for("opus") == "claude"
    assert reg.provider_for("sonnet") == "claude"
    assert reg.provider_for("haiku") == "claude"


def test_registry_provider_for_codex() -> None:
    reg = ModelRegistry()
    assert reg.provider_for("gpt-5.2-codex") == "codex"
    assert reg.provider_for("gpt-5.3-codex") == "codex"
    assert reg.provider_for("o3") == "codex"


def test_registry_resolve_for_provider_native() -> None:
    reg = ModelRegistry()
    model_id, provider = reg.resolve_for_provider("opus", frozenset({"claude"}))
    assert model_id == "opus"
    assert provider == "claude"


def test_registry_resolve_for_provider_fallback() -> None:
    reg = ModelRegistry()
    _model_id, provider = reg.resolve_for_provider("opus", frozenset({"codex"}))
    assert provider == "codex"


def test_registry_resolve_for_provider_no_provider() -> None:
    reg = ModelRegistry()
    with pytest.raises(ValueError, match="No available provider"):
        reg.resolve_for_provider("opus", frozenset())


def test_streaming_config_fields() -> None:
    s = StreamingConfig(enabled=False, min_chars=100)
    assert s.enabled is False
    assert s.min_chars == 100


def test_docker_config_fields() -> None:
    d = DockerConfig(enabled=True, image_name="custom")
    assert d.enabled is True
    assert d.image_name == "custom"
