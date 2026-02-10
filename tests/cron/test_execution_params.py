"""Tests for cron execution with TaskExecutionConfig parameter resolver integration."""

from __future__ import annotations

from unittest.mock import patch

from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cron.execution import build_cmd


class TestBuildCmdWithTaskExecutionConfig:
    """Test build_cmd() with new TaskExecutionConfig signature."""

    def test_build_cmd_claude_basic(self) -> None:
        """Claude command builds correctly with TaskExecutionConfig."""
        exec_config = TaskExecutionConfig(
            provider="claude",
            model="opus",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )

        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            cmd = build_cmd(exec_config, "hello world")

        assert cmd is not None
        assert cmd[0] == "/usr/bin/claude"
        assert "--model" in cmd
        assert "opus" in cmd
        assert "--permission-mode" in cmd
        assert "bypassPermissions" in cmd
        assert "--no-session-persistence" in cmd
        assert cmd[-1] == "hello world"
        assert cmd[-2] == "--"

    def test_build_cmd_claude_with_parameters(self) -> None:
        """Claude command includes extra CLI parameters."""
        exec_config = TaskExecutionConfig(
            provider="claude",
            model="sonnet",
            reasoning_effort="",
            cli_parameters=["--fast", "--verbose"],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )

        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            cmd = build_cmd(exec_config, "test prompt")

        assert cmd is not None
        # Extra parameters should be after standard flags but before --
        assert "--fast" in cmd
        assert "--verbose" in cmd
        # Verify they come before the -- separator
        separator_idx = cmd.index("--")
        assert cmd.index("--fast") < separator_idx
        assert cmd.index("--verbose") < separator_idx

    def test_build_cmd_codex_basic(self) -> None:
        """Codex command builds correctly with TaskExecutionConfig."""
        exec_config = TaskExecutionConfig(
            provider="codex",
            model="gpt-5.2-codex",
            reasoning_effort="medium",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )

        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
            cmd = build_cmd(exec_config, "hello world")

        assert cmd is not None
        assert cmd[0] == "/usr/bin/codex"
        assert "exec" in cmd
        assert "--model" in cmd
        assert "gpt-5.2-codex" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        # Medium is default, so no reasoning effort flag should be added
        assert "model_reasoning_effort" not in " ".join(cmd)

    def test_build_cmd_codex_with_parameters(self) -> None:
        """Codex command includes extra CLI parameters."""
        exec_config = TaskExecutionConfig(
            provider="codex",
            model="gpt-5.1-codex-mini",
            reasoning_effort="medium",
            cli_parameters=["--no-cache", "--debug"],
            permission_mode="full_auto",
            working_dir="/tmp",
            file_access="all",
        )

        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
            cmd = build_cmd(exec_config, "test prompt")

        assert cmd is not None
        assert "--no-cache" in cmd
        assert "--debug" in cmd
        # Parameters should be before the -- separator
        separator_idx = cmd.index("--")
        assert cmd.index("--no-cache") < separator_idx
        assert cmd.index("--debug") < separator_idx
        # Should use --full-auto instead of bypass
        assert "--full-auto" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd

    def test_build_cmd_codex_reasoning_effort_high(self) -> None:
        """Codex command includes reasoning effort flag when non-default."""
        exec_config = TaskExecutionConfig(
            provider="codex",
            model="gpt-5.2-codex",
            reasoning_effort="high",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )

        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
            cmd = build_cmd(exec_config, "complex task")

        assert cmd is not None
        # Should have -c flag with reasoning effort config
        assert "-c" in cmd
        config_idx = cmd.index("-c")
        assert cmd[config_idx + 1] == "model_reasoning_effort=high"

    def test_build_cmd_codex_reasoning_effort_low(self) -> None:
        """Codex command includes reasoning effort flag for low effort."""
        exec_config = TaskExecutionConfig(
            provider="codex",
            model="gpt-5.1-codex-mini",
            reasoning_effort="low",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )

        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
            cmd = build_cmd(exec_config, "quick task")

        assert cmd is not None
        assert "-c" in cmd
        config_idx = cmd.index("-c")
        assert cmd[config_idx + 1] == "model_reasoning_effort=low"

    def test_build_cmd_parameter_order(self) -> None:
        """CLI parameters should appear before -- separator."""
        exec_config = TaskExecutionConfig(
            provider="claude",
            model="opus",
            reasoning_effort="",
            cli_parameters=["--param1", "--param2", "--param3"],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )

        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            cmd = build_cmd(exec_config, "my prompt")

        # Find the -- separator
        separator_idx = cmd.index("--")
        prompt_idx = cmd.index("my prompt")

        # -- should be right before the prompt
        assert prompt_idx == separator_idx + 1

        # All parameters should be before --
        for param in ["--param1", "--param2", "--param3"]:
            param_idx = cmd.index(param)
            assert param_idx < separator_idx

    def test_build_cmd_empty_parameters(self) -> None:
        """Empty parameter list should work correctly."""
        exec_config = TaskExecutionConfig(
            provider="claude",
            model="haiku",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )

        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            cmd = build_cmd(exec_config, "test")

        assert cmd is not None
        # Should still have standard structure
        assert "--no-session-persistence" in cmd
        assert "--" in cmd
        assert "test" in cmd

    def test_build_cmd_cli_not_found(self) -> None:
        """Returns None when CLI binary not found."""
        exec_config = TaskExecutionConfig(
            provider="claude",
            model="opus",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )

        with patch("ductor_bot.cron.execution.which", return_value=None):
            cmd = build_cmd(exec_config, "test")

        assert cmd is None

    def test_build_cmd_codex_with_reasoning_and_parameters(self) -> None:
        """Codex command with both reasoning effort and CLI parameters."""
        exec_config = TaskExecutionConfig(
            provider="codex",
            model="gpt-5.2-codex",
            reasoning_effort="high",
            cli_parameters=["--verbose", "--no-cache"],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )

        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
            cmd = build_cmd(exec_config, "complex task")

        assert cmd is not None
        # Should have reasoning effort config
        assert "-c" in cmd
        config_idx = cmd.index("-c")
        assert cmd[config_idx + 1] == "model_reasoning_effort=high"

        # Should have CLI parameters
        assert "--verbose" in cmd
        assert "--no-cache" in cmd

        # All should be before -- separator
        separator_idx = cmd.index("--")
        assert cmd.index("-c") < separator_idx
        assert cmd.index("--verbose") < separator_idx
        assert cmd.index("--no-cache") < separator_idx
