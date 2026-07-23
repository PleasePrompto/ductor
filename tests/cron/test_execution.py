"""Tests for cron/execution.py: CLI command building and output parsing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cron.execution import (
    OneShotCommand,
    build_cmd,
    enrich_instruction,
    execute_one_shot,
    parse_claude_result,
    parse_codex_result,
    parse_gemini_result,
    parse_result,
)


class TestBuildCmd:
    def test_claude_provider(self) -> None:
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
            result = build_cmd(exec_config, "hello")
        assert result is not None
        assert result.cmd[0] == "/usr/bin/claude"
        assert "--no-session-persistence" in result.cmd
        # Claude: prompt as CLI arg, no stdin
        assert result.stdin_input is None
        assert result.cmd[-1] == "hello"
        assert result.cmd[-2] == "--"

    def test_codex_provider(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="codex",
            model="gpt-4",
            reasoning_effort="medium",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
            result = build_cmd(exec_config, "hello")
        assert result is not None
        assert result.cmd[0] == "/usr/bin/codex"
        assert "--dangerously-bypass-approvals-and-sandbox" in result.cmd
        # Codex: prompt as CLI arg, no stdin
        assert result.stdin_input is None

    def test_codex_full_auto(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="codex",
            model="gpt-4",
            reasoning_effort="medium",
            cli_parameters=[],
            permission_mode="plan",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
            result = build_cmd(exec_config, "hello")
        assert result is not None
        assert "--full-auto" in result.cmd

    def test_returns_none_when_cli_missing(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="claude",
            model="opus",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="plan",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.which", return_value=None):
            assert build_cmd(exec_config, "hello") is None

    def test_gemini_provider(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="gemini",
            model="gemini-2.5-pro",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.find_gemini_cli", return_value="/usr/bin/gemini"):
            result = build_cmd(exec_config, "hello")
        assert result is not None
        assert result.cmd[0] == "/usr/bin/gemini"
        assert "--approval-mode" in result.cmd
        assert "yolo" in result.cmd
        # Hybrid mode: -p "" instead of -- prompt
        assert "-p" in result.cmd
        p_idx = result.cmd.index("-p")
        assert result.cmd[p_idx + 1] == ""
        assert "--" not in result.cmd
        assert "hello" not in result.cmd
        # Prompt via stdin
        assert result.stdin_input == b"hello"

    def test_gemini_returns_none_when_cli_missing(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="gemini",
            model="gemini-2.5-pro",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="plan",
            working_dir="/tmp",
            file_access="all",
        )
        with patch(
            "ductor_bot.cron.execution.find_gemini_cli",
            side_effect=FileNotFoundError("not found"),
        ):
            assert build_cmd(exec_config, "hello") is None

    def test_unknown_provider_falls_back_to_claude(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="unknown",
            model="model",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="plan",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            result = build_cmd(exec_config, "hello")
        assert result is not None
        assert result.cmd[0] == "/usr/bin/claude"
        assert result.stdin_input is None


class TestExecuteOneShotStdin:
    """Test execute_one_shot stdin_input parameter."""

    async def test_with_stdin_input_uses_pipe(self) -> None:
        """stdin_input is forwarded to subprocess via PIPE."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            proc = AsyncMock()
            proc.communicate.return_value = (b'{"result":"ok"}', b"")
            proc.returncode = 0
            mock_exec.return_value = proc

            result = await execute_one_shot(
                OneShotCommand(cmd=["/usr/bin/gemini", "-p", ""], stdin_input=b"hello"),
                cwd=Path("/tmp"),
                provider="gemini",
                timeout_seconds=60,
                timeout_label="Test",
            )

        call_kwargs = mock_exec.call_args[1]
        assert call_kwargs["stdin"] == asyncio.subprocess.PIPE
        proc.communicate.assert_called_once_with(input=b"hello")
        assert result.status == "success"

    async def test_without_stdin_input_uses_devnull(self) -> None:
        """Without stdin_input, DEVNULL is used (backward compat)."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            proc = AsyncMock()
            proc.communicate.return_value = (b'{"result":"ok"}', b"")
            proc.returncode = 0
            mock_exec.return_value = proc

            await execute_one_shot(
                OneShotCommand(cmd=["/usr/bin/claude", "-p", "--", "hello"]),
                cwd=Path("/tmp"),
                provider="claude",
                timeout_seconds=60,
                timeout_label="Test",
            )

        call_kwargs = mock_exec.call_args[1]
        assert call_kwargs["stdin"] == asyncio.subprocess.DEVNULL
        proc.communicate.assert_called_once_with(input=None)

    async def test_env_overrides_are_merged_into_process_environment(self) -> None:
        """Command-specific env overrides are passed without dropping host env."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            proc = AsyncMock()
            proc.communicate.return_value = (b'{"result":"ok"}', b"")
            proc.returncode = 0
            mock_exec.return_value = proc

            await execute_one_shot(
                OneShotCommand(
                    cmd=["/usr/bin/claude", "-p", "--", "hello"],
                    env_overrides={"DUCTOR_HOME": "/tmp/agent-home"},
                ),
                cwd=Path("/tmp"),
                provider="claude",
                timeout_seconds=60,
                timeout_label="Test",
            )

        env = mock_exec.call_args[1]["env"]
        assert env["DUCTOR_HOME"] == "/tmp/agent-home"
        assert "PATH" in env


class TestEnrichInstruction:
    def test_appends_memory_instructions(self) -> None:
        result = enrich_instruction("Do the work", "daily-report")
        assert "daily-report_MEMORY.md" in result
        assert "Do the work" in result

    def test_preserves_original(self) -> None:
        original = "Original instruction"
        result = enrich_instruction(original, "weekly")
        assert result.startswith(original)


class TestParseClaude:
    def test_parses_json(self) -> None:
        import json

        stdout = json.dumps({"result": "Hello world"}).encode()
        assert parse_claude_result(stdout) == "Hello world"

    def test_empty_bytes(self) -> None:
        assert parse_claude_result(b"") == ""

    def test_non_json_returns_raw(self) -> None:
        raw = b"Some raw text output"
        assert parse_claude_result(raw) == "Some raw text output"


class TestParseCodex:
    def test_empty_bytes(self) -> None:
        assert parse_codex_result(b"") == ""

    def test_parsed_jsonl_with_no_text_returns_empty(self) -> None:
        """Silent-success: valid JSONL events but no assistant text -> empty."""
        raw = (
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"turn.started"}\n'
            '{"type":"item.started","item":{"type":"command_execution"}}\n'
            '{"type":"item.completed","item":{"type":"command_execution"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10}}\n'
        )
        assert parse_codex_result(raw.encode()) == ""

    def test_parsed_jsonl_with_text_returns_text(self) -> None:
        raw = (
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"Hello"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10}}\n'
        )
        assert parse_codex_result(raw.encode()) == "Hello"

    def test_non_jsonl_returns_raw(self) -> None:
        raw = b"Plain text output from codex"
        assert parse_codex_result(raw) == "Plain text output from codex"


class TestParseGemini:
    def test_empty_bytes(self) -> None:
        assert parse_gemini_result(b"") == ""

    def test_json_response(self) -> None:
        import json

        data = json.dumps([{"type": "message", "role": "model", "content": "Result text"}])
        result = parse_gemini_result(data.encode())
        assert "Result text" in result

    def test_non_json_returns_raw(self) -> None:
        raw = b"Raw gemini output"
        assert parse_gemini_result(raw) == "Raw gemini output"


class TestParseResult:
    def test_dispatches_to_gemini_parser(self) -> None:
        assert parse_result("gemini", b'{"result":"ok"}') == "ok"

    def test_unknown_provider_falls_back_to_claude(self) -> None:
        assert parse_result("unknown", b'{"result":"fallback"}') == "fallback"
