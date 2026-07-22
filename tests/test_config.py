"""Tests for config and model registry."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ductor_bot.config import (
    AgentConfig,
    DockerConfig,
    MemoryCompactionConfig,
    MemoryFlushConfig,
    MemoryReflectionConfig,
    ModelRegistry,
    StreamingConfig,
    deep_merge_config,
    reset_gemini_models,
    update_config_file,
)

# -- AgentConfig defaults --


@pytest.fixture(autouse=True)
def _reset_gemini_models() -> None:
    reset_gemini_models()


def test_agent_config_defaults() -> None:
    cfg = AgentConfig()
    assert cfg.provider == "claude"
    assert cfg.model == "opus"
    assert cfg.idle_timeout_minutes == 1440
    assert cfg.daily_reset_hour == 4
    assert cfg.cli_timeout == 1800.0
    assert cfg.permission_mode == "bypassPermissions"
    assert cfg.gemini_api_key is None
    assert cfg.telegram_token == ""
    assert cfg.allowed_user_ids == []
    assert cfg.cron_delivery_retry.enabled is False
    assert cfg.cron_delivery_retry.interval_seconds == 300
    assert cfg.cron_delivery_retry.max_attempts == 12
    assert cfg.cron_preflight.enabled is False
    assert cfg.cron_preflight.timeout_seconds == 15.0
    assert cfg.cron_preflight.skip_marker == "HEARTBEAT_OK"


def test_agent_config_normalizes_nullish_gemini_api_key() -> None:
    assert AgentConfig(gemini_api_key="null").gemini_api_key is None
    assert AgentConfig(gemini_api_key=" NONE ").gemini_api_key is None
    assert AgentConfig(gemini_api_key="   ").gemini_api_key is None


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


def test_update_config_file_skips_unchanged_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"provider": "codex", "model": "gpt-5.1-codex"}\n', encoding="utf-8")
    writes = 0

    def fake_atomic_json_save(_path: Path, _data: object) -> None:
        nonlocal writes
        writes += 1

    monkeypatch.setattr("ductor_bot.infra.json_store.atomic_json_save", fake_atomic_json_save)

    update_config_file(config_path, provider="codex", model="gpt-5.1-codex")

    assert writes == 0
    assert config_path.read_text(encoding="utf-8") == (
        '{"provider": "codex", "model": "gpt-5.1-codex"}\n'
    )


# -- ModelRegistry --


def test_registry_provider_for_claude() -> None:
    reg = ModelRegistry()
    assert reg.provider_for("opus") == "claude"
    assert reg.provider_for("sonnet") == "claude"
    assert reg.provider_for("haiku") == "claude"


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ],
)
def test_registry_provider_for_full_claude_model_ids(model_id: str) -> None:
    reg = ModelRegistry()
    assert reg.provider_for(model_id) == "claude"


def test_registry_provider_for_codex() -> None:
    reg = ModelRegistry()
    assert reg.provider_for("gpt-5.2-codex") == "codex"
    assert reg.provider_for("gpt-5.3-codex") == "codex"
    assert reg.provider_for("o3") == "codex"


def test_registry_provider_for_gemini_prefix() -> None:
    reg = ModelRegistry()
    reset_gemini_models()
    assert reg.provider_for("gemini-2.5-pro") == "gemini"


def test_registry_provider_for_antigravity() -> None:
    reg = ModelRegistry()
    assert reg.provider_for("antigravity-default") == "antigravity"


def test_streaming_config_fields() -> None:
    s = StreamingConfig(enabled=False, min_chars=100)
    assert s.enabled is False
    assert s.min_chars == 100


def test_docker_config_fields() -> None:
    d = DockerConfig(enabled=True, image_name="custom")
    assert d.enabled is True
    assert d.image_name == "custom"


# -- AgentConfig transports normalization --


def test_transport_backward_compat_populates_transports() -> None:
    """Legacy single ``transport`` field fills ``transports`` list."""
    cfg = AgentConfig(transport="telegram")
    assert cfg.transports == ["telegram"]
    assert cfg.transport == "telegram"


def test_transport_matrix_backward_compat() -> None:
    """transport='matrix' with empty transports normalizes correctly."""
    cfg = AgentConfig(transport="matrix")
    assert cfg.transports == ["matrix"]
    assert cfg.transport == "matrix"


def test_transport_slack_backward_compat() -> None:
    """transport='slack' with empty transports normalizes correctly."""
    cfg = AgentConfig(transport="slack")
    assert cfg.transports == ["slack"]
    assert cfg.transport == "slack"


def test_transports_multi_sets_primary_transport() -> None:
    """Explicit multi-transport sets ``transport`` to first entry."""
    cfg = AgentConfig(transports=["telegram", "matrix"])
    assert cfg.transports == ["telegram", "matrix"]
    assert cfg.transport == "telegram"


def test_transports_multi_reversed_order() -> None:
    """Primary transport is always the first in the list."""
    cfg = AgentConfig(transports=["matrix", "telegram"])
    assert cfg.transport == "matrix"


def test_is_multi_transport_single() -> None:
    cfg = AgentConfig(transport="telegram")
    assert cfg.is_multi_transport is False


def test_is_multi_transport_multiple() -> None:
    cfg = AgentConfig(transports=["telegram", "matrix"])
    assert cfg.is_multi_transport is True


def test_transports_default_is_telegram() -> None:
    """Default AgentConfig has transports=['telegram']."""
    cfg = AgentConfig()
    assert cfg.transports == ["telegram"]


# -- Memory* config bounds (MED #4) --


def test_memory_reflection_rejects_zero_every_n_messages() -> None:
    """``every_n_messages=0`` would trigger ZeroDivisionError in hooks.py modulo check."""
    with pytest.raises(ValidationError, match="every_n_messages"):
        MemoryReflectionConfig(every_n_messages=0)


def test_memory_reflection_rejects_negative_every_n_messages() -> None:
    """Negative cadence is nonsense."""
    with pytest.raises(ValidationError, match="every_n_messages"):
        MemoryReflectionConfig(every_n_messages=-5)


def test_memory_flush_rejects_negative_dedup_seconds() -> None:
    """``dedup_seconds`` accepts 0 (no window) but rejects negative values."""
    with pytest.raises(ValidationError, match="dedup_seconds"):
        MemoryFlushConfig(dedup_seconds=-1)


def test_memory_flush_accepts_zero_dedup_seconds() -> None:
    """0 means no dedup window -- flush on every boundary."""
    cfg = MemoryFlushConfig(dedup_seconds=0)
    assert cfg.dedup_seconds == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trigger_lines", 0),
        ("trigger_lines", -10),
        ("target_lines", 0),
        ("target_lines", -3),
    ],
)
def test_memory_compaction_rejects_nonpositive_line_counts(field: str, value: int) -> None:
    """``trigger_lines`` / ``target_lines`` must be >= 1."""
    with pytest.raises(ValidationError, match=field):
        MemoryCompactionConfig(**{field: value})


def test_memory_compaction_rejects_negative_preserve_recency_days() -> None:
    """``preserve_recency_days`` accepts 0 (disabled) but rejects negative."""
    with pytest.raises(ValidationError, match="preserve_recency_days"):
        MemoryCompactionConfig(preserve_recency_days=-1)


def test_memory_compaction_rejects_target_gt_trigger() -> None:
    """``target_lines`` must not exceed ``trigger_lines`` (compaction no-op)."""
    with pytest.raises(ValidationError, match="target_lines"):
        MemoryCompactionConfig(trigger_lines=40, target_lines=70)


def test_memory_compaction_accepts_target_eq_trigger() -> None:
    """``target_lines == trigger_lines`` is allowed (boundary case)."""
    cfg = MemoryCompactionConfig(trigger_lines=50, target_lines=50)
    assert cfg.target_lines == 50
    assert cfg.trigger_lines == 50


def test_update_config_file_does_not_log_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Config values (api tokens, keys) must never reach the log (#175 follow-up)."""
    from ductor_bot.config import update_config_file

    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    with caplog.at_level("DEBUG", logger="ductor_bot.config"):
        update_config_file(config_path, api={"token": "SUPERSECRET-TOKEN"})
        update_config_file(config_path, api={"token": "SUPERSECRET-TOKEN"})  # unchanged path

    assert "SUPERSECRET-TOKEN" not in caplog.text
    assert "api" in caplog.text
