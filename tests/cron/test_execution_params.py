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
            result = build_cmd(exec_config, "hello world")

        assert result is not None
        assert result.cmd[0] == "/usr/bin/claude"
        assert "--model" in result.cmd
        assert "opus" in result.cmd
        assert "--permission-mode" in result.cmd
        assert "bypassPermissions" in result.cmd
        assert "--no-session-persistence" in result.cmd
        assert result.cmd[-1] == "hello world"
        assert result.cmd[-2] == "--"
        assert result.stdin_input is None

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
            result = build_cmd(exec_config, "test prompt")

        assert result is not None
        # Extra parameters should be after standard flags but before --
        assert "--fast" in result.cmd
        assert "--verbose" in result.cmd
        # Verify they come before the -- separator
        separator_idx = result.cmd.index("--")
        assert result.cmd.index("--fast") < separator_idx
        assert result.cmd.index("--verbose") < separator_idx

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
            result = build_cmd(exec_config, "hello world")

        assert result is not None
        assert result.cmd[0] == "/usr/bin/codex"
        assert "exec" in result.cmd
        assert "--model" in result.cmd
        assert "gpt-5.2-codex" in result.cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in result.cmd
        # Medium is default, so no reasoning effort flag should be added
        assert "model_reasoning_effort" not in " ".join(result.cmd)
        assert result.stdin_input is None

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
            result = build_cmd(exec_config, "test prompt")

        assert result is not None
        assert "--no-cache" in result.cmd
        assert "--debug" in result.cmd
        # Parameters should be before the -- separator
        separator_idx = result.cmd.index("--")
        assert result.cmd.index("--no-cache") < separator_idx
        assert result.cmd.index("--debug") < separator_idx
        # Should use --full-auto instead of bypass
        assert "--full-auto" in result.cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in result.cmd

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
            result = build_cmd(exec_config, "complex task")

        assert result is not None
        # Should have -c flag with reasoning effort config
        assert "-c" in result.cmd
        config_idx = result.cmd.index("-c")
        assert result.cmd[config_idx + 1] == "model_reasoning_effort=high"

    def test_build_cmd_claude_reasoning_effort(self) -> None:
        """Claude command emits --effort (not the codex -c flag)."""
        exec_config = TaskExecutionConfig(
            provider="claude",
            model="opus",
            reasoning_effort="max",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            result = build_cmd(exec_config, "deep task")
        assert result is not None
        assert "--effort" in result.cmd
        assert result.cmd[result.cmd.index("--effort") + 1] == "max"
        assert "model_reasoning_effort" not in " ".join(result.cmd)

    def test_build_cmd_claude_skips_effort_on_default(self) -> None:
        """Claude command omits --effort for the "default" sentinel (CLI default)."""
        exec_config = TaskExecutionConfig(
            provider="claude",
            model="opus",
            reasoning_effort="default",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            result = build_cmd(exec_config, "task")
        assert result is not None
        assert "--effort" not in result.cmd

    def test_build_cmd_claude_emits_explicit_medium(self) -> None:
        """An explicitly configured medium effort is passed through, not dropped."""
        exec_config = TaskExecutionConfig(
            provider="claude",
            model="opus",
            reasoning_effort="medium",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            result = build_cmd(exec_config, "task")
        assert result is not None
        assert result.cmd[result.cmd.index("--effort") + 1] == "medium"

    def test_build_cmd_gemini_emits_no_effort(self) -> None:
        """Gemini command emits neither --effort nor the codex -c flag."""
        exec_config = TaskExecutionConfig(
            provider="gemini",
            model="gemini-2.5-pro",
            reasoning_effort="high",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.find_gemini_cli", return_value="/usr/bin/gemini"):
            result = build_cmd(exec_config, "task")
        if result is not None:
            joined = " ".join(result.cmd)
            assert "--effort" not in result.cmd
            assert "model_reasoning_effort" not in joined

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
            result = build_cmd(exec_config, "quick task")

        assert result is not None
        assert "-c" in result.cmd
        config_idx = result.cmd.index("-c")
        assert result.cmd[config_idx + 1] == "model_reasoning_effort=low"

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
            result = build_cmd(exec_config, "my prompt")

        # Find the -- separator
        separator_idx = result.cmd.index("--")
        prompt_idx = result.cmd.index("my prompt")

        # -- should be right before the prompt
        assert prompt_idx == separator_idx + 1

        # All parameters should be before --
        for param in ["--param1", "--param2", "--param3"]:
            param_idx = result.cmd.index(param)
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
            result = build_cmd(exec_config, "test")

        assert result is not None
        # Should still have standard structure
        assert "--no-session-persistence" in result.cmd
        assert "--" in result.cmd
        assert "test" in result.cmd

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
            result = build_cmd(exec_config, "test")

        assert result is None

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
            result = build_cmd(exec_config, "complex task")

        assert result is not None
        # Should have reasoning effort config
        assert "-c" in result.cmd
        config_idx = result.cmd.index("-c")
        assert result.cmd[config_idx + 1] == "model_reasoning_effort=high"

        # Should have CLI parameters
        assert "--verbose" in result.cmd
        assert "--no-cache" in result.cmd

        # All should be before -- separator
        separator_idx = result.cmd.index("--")
        assert result.cmd.index("-c") < separator_idx
        assert result.cmd.index("--verbose") < separator_idx
        assert result.cmd.index("--no-cache") < separator_idx


class TestResolveToBuildEffortFlow:
    """End-to-end: resolve_cli_config -> build_cmd must deliver effort to the CLI."""

    def _base(self, **kw):
        from ductor_bot.config import AgentConfig

        return AgentConfig(provider="claude", model="opus", reasoning_effort="high", **kw)

    def _codex_cache(self):
        from ductor_bot.cli.codex_cache import CodexModelCache
        from ductor_bot.cli.codex_discovery import CodexModelInfo

        return CodexModelCache(
            last_updated="2026-02-10T12:00:00",
            models=[
                CodexModelInfo(
                    id="gpt-5.2-codex",
                    display_name="c",
                    description="d",
                    supported_efforts=("low", "medium", "high", "xhigh"),
                    default_effort="medium",
                    is_default=True,
                )
            ],
        )

    def test_claude_effort_reaches_cli(self) -> None:
        from ductor_bot.cli.param_resolver import resolve_cli_config

        cfg = resolve_cli_config(self._base(), self._codex_cache())
        assert cfg.reasoning_effort == "high"  # not dropped by resolve
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            result = build_cmd(cfg, "p")
        assert result is not None
        assert "--effort" in result.cmd
        assert result.cmd[result.cmd.index("--effort") + 1] == "high"
        assert "model_reasoning_effort" not in " ".join(result.cmd)

    def test_codex_effort_reaches_cli(self) -> None:
        from ductor_bot.cli.param_resolver import TaskOverrides, resolve_cli_config

        cfg = resolve_cli_config(
            self._base(),
            self._codex_cache(),
            task_overrides=TaskOverrides(
                provider="codex", model="gpt-5.2-codex", reasoning_effort="high"
            ),
        )
        assert cfg.reasoning_effort == "high"
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
            result = build_cmd(cfg, "p")
        assert result is not None
        assert "-c" in result.cmd
        assert "model_reasoning_effort=high" in result.cmd
        assert "--effort" not in result.cmd

    def test_gemini_drops_effort(self) -> None:
        from ductor_bot.cli.param_resolver import TaskOverrides, resolve_cli_config
        from ductor_bot.config import set_gemini_models

        set_gemini_models(frozenset({"gemini-2.5-pro"}))
        cfg = resolve_cli_config(
            self._base(),
            self._codex_cache(),
            task_overrides=TaskOverrides(provider="gemini", model="gemini-2.5-pro"),
        )
        assert cfg.reasoning_effort == ""
