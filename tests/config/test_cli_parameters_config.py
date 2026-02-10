"""Test CLI parameters config schema."""

from __future__ import annotations

import json
from pathlib import Path

from ductor_bot.config import AgentConfig, CLIParametersConfig, deep_merge_config


def test_cli_parameters_config_defaults() -> None:
    """CLIParametersConfig should have empty lists as defaults."""
    config = CLIParametersConfig()
    assert config.claude == []
    assert config.codex == []


def test_cli_parameters_config_with_values() -> None:
    """CLIParametersConfig should accept and store values."""
    config = CLIParametersConfig(
        claude=["--fast", "--no-cache"],
        codex=["--verbose", "--debug"],
    )
    assert config.claude == ["--fast", "--no-cache"]
    assert config.codex == ["--verbose", "--debug"]


def test_agent_config_includes_cli_parameters() -> None:
    """AgentConfig should include cli_parameters field with default factory."""
    config = AgentConfig()
    assert hasattr(config, "cli_parameters")
    assert isinstance(config.cli_parameters, CLIParametersConfig)
    assert config.cli_parameters.claude == []
    assert config.cli_parameters.codex == []


def test_agent_config_with_cli_parameters() -> None:
    """AgentConfig should accept cli_parameters during initialization."""
    config = AgentConfig(
        cli_parameters=CLIParametersConfig(
            claude=["--fast"],
            codex=["--verbose"],
        ),
    )
    assert config.cli_parameters.claude == ["--fast"]
    assert config.cli_parameters.codex == ["--verbose"]


def test_agent_config_json_round_trip_with_cli_parameters() -> None:
    """AgentConfig with cli_parameters should serialize and deserialize correctly."""
    original = AgentConfig(
        cli_parameters=CLIParametersConfig(
            claude=["--fast", "--no-cache"],
            codex=["--verbose"],
        ),
    )

    # Serialize
    data = original.model_dump()
    json_str = json.dumps(data)

    # Deserialize
    parsed_data = json.loads(json_str)
    restored = AgentConfig(**parsed_data)

    assert restored.cli_parameters.claude == ["--fast", "--no-cache"]
    assert restored.cli_parameters.codex == ["--verbose"]


def test_deep_merge_preserves_cli_parameters() -> None:
    """deep_merge_config should preserve user CLI parameters and add new fields."""
    user_config = {
        "cli_parameters": {
            "claude": ["--fast"],
            "codex": ["--verbose"],
        },
    }

    defaults = AgentConfig().model_dump()

    merged, _changed = deep_merge_config(user_config, defaults)

    # User values should be preserved
    assert merged["cli_parameters"]["claude"] == ["--fast"]
    assert merged["cli_parameters"]["codex"] == ["--verbose"]

    # New top-level fields should be added
    assert "log_level" in merged
    assert "provider" in merged


def test_backward_compatibility_without_cli_parameters() -> None:
    """Old config without cli_parameters field should load with defaults."""
    old_config_dict = {
        "log_level": "DEBUG",
        "provider": "claude",
        "model": "opus",
    }

    # Merge with defaults
    defaults = AgentConfig().model_dump()
    merged, changed = deep_merge_config(old_config_dict, defaults)

    # Should add cli_parameters with defaults
    assert "cli_parameters" in merged
    assert merged["cli_parameters"]["claude"] == []
    assert merged["cli_parameters"]["codex"] == []
    assert changed is True

    # User values should be preserved
    assert merged["log_level"] == "DEBUG"
    assert merged["provider"] == "claude"


def test_deep_merge_nested_cli_parameters() -> None:
    """deep_merge_config should merge nested cli_parameters correctly."""
    # User only specified claude params
    user_config = {
        "cli_parameters": {
            "claude": ["--fast"],
        },
    }

    defaults = AgentConfig().model_dump()

    merged, changed = deep_merge_config(user_config, defaults)

    # User's claude should be preserved
    assert merged["cli_parameters"]["claude"] == ["--fast"]

    # Missing codex should be added from defaults
    assert "codex" in merged["cli_parameters"]
    assert merged["cli_parameters"]["codex"] == []
    assert changed is True


def test_config_file_round_trip(tmp_path: Path) -> None:
    """Config with cli_parameters should persist correctly to disk."""
    config_path = tmp_path / "config.json"

    # Create config with cli_parameters
    config = AgentConfig(
        provider="codex",
        model="gpt-5.2-codex",
        cli_parameters=CLIParametersConfig(
            claude=["--fast"],
            codex=["--verbose", "--debug"],
        ),
    )

    # Write to file
    config_dict = config.model_dump()
    config_path.write_text(json.dumps(config_dict, indent=2))

    # Read back
    loaded_dict = json.loads(config_path.read_text())
    loaded_config = AgentConfig(**loaded_dict)

    assert loaded_config.provider == "codex"
    assert loaded_config.model == "gpt-5.2-codex"
    assert loaded_config.cli_parameters.claude == ["--fast"]
    assert loaded_config.cli_parameters.codex == ["--verbose", "--debug"]


def test_empty_cli_parameters_list_vs_none() -> None:
    """Empty list should be preserved, not treated as None."""
    config = AgentConfig(
        cli_parameters=CLIParametersConfig(
            claude=[],
            codex=[],
        ),
    )

    data = config.model_dump()
    restored = AgentConfig(**data)

    # Empty lists should remain empty lists, not None
    assert restored.cli_parameters.claude == []
    assert restored.cli_parameters.codex == []
    assert isinstance(restored.cli_parameters.claude, list)
    assert isinstance(restored.cli_parameters.codex, list)
