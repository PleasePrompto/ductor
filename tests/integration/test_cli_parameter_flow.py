"""Integration tests for CLI parameter flow through the main agent."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.service import CLIServiceConfig


@pytest.fixture
def service_config_with_params() -> CLIServiceConfig:
    """CLI service config with provider-specific parameters."""
    return CLIServiceConfig(
        working_dir="/workspace",
        default_model="sonnet",
        provider="claude",
        max_turns=10,
        max_budget_usd=None,
        permission_mode="normal",
        reasoning_effort="medium",
        docker_container=None,
        claude_cli_parameters=["--claude-flag", "claude-value"],
        codex_cli_parameters=["--codex-flag", "codex-value"],
    )


def test_main_agent_claude_parameters() -> None:
    """Should pass Claude-specific CLI parameters to Claude provider."""
    from ductor_bot.cli.claude_provider import ClaudeCodeCLI

    config = CLIConfig(
        provider="claude",
        working_dir="/workspace",
        model="sonnet",
        system_prompt="",
        append_system_prompt="",
        max_turns=10,
        max_budget_usd=None,
        permission_mode="normal",
        reasoning_effort="",
        docker_container="",
        process_registry=MagicMock(),
        chat_id=123,
        process_label="test",
        cli_parameters=["--claude-flag", "claude-value"],
    )

    provider = ClaudeCodeCLI(config)
    cmd = provider._build_command("test prompt")

    # Verify Claude parameters are present before --
    separator_idx = cmd.index("--")
    params_before_separator = cmd[:separator_idx]

    assert "--claude-flag" in params_before_separator
    assert "claude-value" in params_before_separator

    # Verify prompt comes after separator
    assert cmd[separator_idx + 1] == "test prompt"


def test_main_agent_codex_parameters() -> None:
    """Should pass Codex-specific CLI parameters to Codex provider."""
    from ductor_bot.cli.codex_provider import CodexCLI

    config = CLIConfig(
        provider="codex",
        working_dir="/workspace",
        model="gpt-4o",
        system_prompt="",
        append_system_prompt="",
        max_turns=10,
        max_budget_usd=None,
        permission_mode="normal",
        reasoning_effort="medium",
        docker_container="",
        process_registry=MagicMock(),
        chat_id=123,
        process_label="test",
        cli_parameters=["--codex-flag", "codex-value"],
    )

    provider = CodexCLI(config)
    cmd = provider._build_command("test prompt")

    # Verify Codex parameters are present before --
    separator_idx = cmd.index("--")
    params_before_separator = cmd[:separator_idx]

    assert "--codex-flag" in params_before_separator
    assert "codex-value" in params_before_separator

    # Verify prompt comes after separator
    assert cmd[separator_idx + 1] == "test prompt"


def test_parameter_isolation() -> None:
    """Should not leak Claude parameters to Codex and vice versa."""
    from ductor_bot.cli.claude_provider import ClaudeCodeCLI
    from ductor_bot.cli.codex_provider import CodexCLI

    # Build Claude command with Claude params
    claude_config = CLIConfig(
        provider="claude",
        working_dir="/workspace",
        model="sonnet",
        system_prompt="",
        append_system_prompt="",
        max_turns=10,
        max_budget_usd=None,
        permission_mode="normal",
        reasoning_effort="",
        docker_container="",
        process_registry=MagicMock(),
        chat_id=123,
        process_label="test",
        cli_parameters=["--claude-flag", "claude-value"],
    )

    claude_provider = ClaudeCodeCLI(claude_config)
    claude_cmd = claude_provider._build_command("test prompt")

    # Verify Claude command doesn't contain Codex params
    assert "--codex-flag" not in claude_cmd
    assert "codex-value" not in " ".join(claude_cmd)

    # Build Codex command with Codex params
    codex_config = CLIConfig(
        provider="codex",
        working_dir="/workspace",
        model="gpt-4o",
        system_prompt="",
        append_system_prompt="",
        max_turns=10,
        max_budget_usd=None,
        permission_mode="normal",
        reasoning_effort="medium",
        docker_container="",
        process_registry=MagicMock(),
        chat_id=123,
        process_label="test",
        cli_parameters=["--codex-flag", "codex-value"],
    )

    codex_provider = CodexCLI(codex_config)
    codex_cmd = codex_provider._build_command("test prompt")

    # Verify Codex command doesn't contain Claude params
    assert "--claude-flag" not in codex_cmd
    assert "claude-value" not in " ".join(codex_cmd)


async def test_cli_service_parameter_routing() -> None:
    """Should route provider-specific parameters through CLIService."""
    config = CLIServiceConfig(
        working_dir="/workspace",
        default_model="sonnet",
        provider="claude",
        max_turns=10,
        max_budget_usd=None,
        permission_mode="normal",
        reasoning_effort="medium",
        docker_container=None,
        claude_cli_parameters=["--claude-param", "value1"],
        codex_cli_parameters=["--codex-param", "value2"],
    )

    # Test Claude parameter routing
    claude_params = config.cli_parameters_for_provider("claude")
    assert claude_params == ["--claude-param", "value1"]

    # Test Codex parameter routing
    codex_params = config.cli_parameters_for_provider("codex")
    assert codex_params == ["--codex-param", "value2"]
