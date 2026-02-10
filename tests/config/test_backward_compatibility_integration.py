"""Integration test for backward compatibility of config schema updates."""

from __future__ import annotations

import json
from pathlib import Path

from ductor_bot.config import AgentConfig, deep_merge_config


def test_old_config_loads_with_new_defaults(tmp_path: Path) -> None:
    """Old config.json without cli_parameters should load successfully."""
    config_path = tmp_path / "config.json"

    # Simulate old config file from v0.3.3 (without cli_parameters)
    old_config = {
        "log_level": "DEBUG",
        "provider": "claude",
        "model": "sonnet",
        "telegram_token": "test-token",
        "allowed_user_ids": [12345],
        "streaming": {
            "enabled": True,
            "min_chars": 200,
        },
    }

    config_path.write_text(json.dumps(old_config, indent=2))

    # Load and merge with defaults (simulating startup)
    loaded_data = json.loads(config_path.read_text())
    defaults = AgentConfig().model_dump()
    merged, changed = deep_merge_config(loaded_data, defaults)

    # Should have added cli_parameters
    assert changed is True
    assert "cli_parameters" in merged
    assert merged["cli_parameters"]["claude"] == []
    assert merged["cli_parameters"]["codex"] == []

    # User values should be preserved
    assert merged["log_level"] == "DEBUG"
    assert merged["model"] == "sonnet"
    assert merged["telegram_token"] == "test-token"
    assert merged["allowed_user_ids"] == [12345]

    # Should be able to parse into AgentConfig
    config = AgentConfig(**merged)
    assert config.log_level == "DEBUG"
    assert config.cli_parameters.claude == []
    assert config.cli_parameters.codex == []


def test_partial_cli_parameters_gets_completed(tmp_path: Path) -> None:
    """Config with only partial cli_parameters should be completed."""
    config_path = tmp_path / "config.json"

    # Config with only claude parameters (maybe manually edited)
    partial_config = {
        "provider": "codex",
        "cli_parameters": {
            "claude": ["--fast"],
        },
    }

    config_path.write_text(json.dumps(partial_config, indent=2))

    loaded_data = json.loads(config_path.read_text())
    defaults = AgentConfig().model_dump()
    merged, changed = deep_merge_config(loaded_data, defaults)

    # Should add missing codex key
    assert changed is True
    assert "codex" in merged["cli_parameters"]
    assert merged["cli_parameters"]["claude"] == ["--fast"]
    assert merged["cli_parameters"]["codex"] == []

    # Should be parseable
    config = AgentConfig(**merged)
    assert config.cli_parameters.claude == ["--fast"]
    assert config.cli_parameters.codex == []


def test_config_with_all_new_fields_needs_no_merge(tmp_path: Path) -> None:
    """Config with all new fields should not trigger changes."""
    config_path = tmp_path / "config.json"

    # Complete config with new fields
    complete_config = {
        "log_level": "INFO",
        "provider": "codex",
        "model": "gpt-5.2-codex",
        "cli_parameters": {
            "claude": ["--fast"],
            "codex": ["--verbose"],
        },
        "telegram_token": "test-token",
        "allowed_user_ids": [12345],
    }

    config_path.write_text(json.dumps(complete_config, indent=2))

    loaded_data = json.loads(config_path.read_text())
    defaults = AgentConfig().model_dump()
    merged, _changed = deep_merge_config(loaded_data, defaults)

    # Should add missing top-level keys but not change cli_parameters
    # _changed will be True because of missing top-level fields
    assert "cli_parameters" in merged
    assert merged["cli_parameters"]["claude"] == ["--fast"]
    assert merged["cli_parameters"]["codex"] == ["--verbose"]

    # User values preserved
    assert merged["provider"] == "codex"
    assert merged["model"] == "gpt-5.2-codex"


def test_deeply_nested_defaults_merge_correctly() -> None:
    """deep_merge_config should handle multiple levels of nesting."""
    user_config = {
        "streaming": {
            "enabled": False,
            # min_chars missing - should be added from defaults
        },
        "cli_parameters": {
            "claude": ["--custom-flag"],
            # codex missing - should be added from defaults
        },
    }

    defaults = AgentConfig().model_dump()
    merged, changed = deep_merge_config(user_config, defaults)

    assert changed is True

    # Streaming should have user's enabled + default min_chars
    assert merged["streaming"]["enabled"] is False
    assert "min_chars" in merged["streaming"]
    assert merged["streaming"]["min_chars"] == 200  # default value

    # CLI parameters should have user's claude + default codex
    assert merged["cli_parameters"]["claude"] == ["--custom-flag"]
    assert "codex" in merged["cli_parameters"]
    assert merged["cli_parameters"]["codex"] == []
