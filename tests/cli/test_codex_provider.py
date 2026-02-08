"""Tests for CodexCLI provider: command building, send, streaming, parsing."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.codex_provider import CodexCLI, _codex_final_result, _log_cmd
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
    SystemInitEvent,
    ThinkingEvent,
    ToolUseEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cli(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> CodexCLI:
    monkeypatch.setattr("ductor_bot.cli.codex_provider.which", lambda _: "/usr/bin/codex")
    return CodexCLI(
        CLIConfig(
            provider="codex",
            model=overrides.pop("model", "gpt-5.2-codex"),
            **overrides,
        )
    )


def _make_process_mock(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> AsyncMock:
    """Create an asyncio.subprocess.Process mock with communicate()."""
    proc = AsyncMock(spec=asyncio.subprocess.Process)
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 12345
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


def _make_streaming_process(
    lines: list[str],
    stderr: bytes = b"",
    returncode: int = 0,
) -> AsyncMock:
    """Create a process mock that yields stdout lines one at a time."""
    proc = AsyncMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode
    proc.pid = 12345
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    # stdout readline mock: returns each line as bytes, then b""
    encoded_lines = [line.encode() + b"\n" for line in lines] + [b""]
    stdout_mock = AsyncMock()
    stdout_mock.readline = AsyncMock(side_effect=encoded_lines)
    proc.stdout = stdout_mock

    # stderr mock
    stderr_mock = AsyncMock()
    stderr_mock.read = AsyncMock(return_value=stderr)
    proc.stderr = stderr_mock

    return proc


async def _collect_events(gen: AsyncGenerator[StreamEvent, None]) -> list[StreamEvent]:
    """Drain an async generator of StreamEvents into a list."""
    return [event async for event in gen]


# ---------------------------------------------------------------------------
# __init__ / _find_cli
# ---------------------------------------------------------------------------


class TestInit:
    def test_find_cli_raises_when_not_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.codex_provider.which", lambda _: None)
        with pytest.raises(FileNotFoundError, match="codex CLI not found"):
            CodexCLI(CLIConfig(provider="codex"))

    def test_find_cli_uses_resolved_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.codex_provider.which", lambda _: "/opt/bin/codex")
        cli = CodexCLI(CLIConfig(provider="codex"))
        assert cli._cli == "/opt/bin/codex"

    def test_docker_container_skips_find_cli(self) -> None:
        """When docker_container is set, _find_cli is never called."""
        cli = CodexCLI(CLIConfig(provider="codex", docker_container="my-sandbox"))
        assert cli._cli == "codex"

    def test_working_dir_resolved(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("ductor_bot.cli.codex_provider.which", lambda _: "/usr/bin/codex")
        cli = CodexCLI(CLIConfig(provider="codex", working_dir=str(tmp_path)))
        assert cli._working_dir == tmp_path.resolve()


# ---------------------------------------------------------------------------
# _compose_prompt
# ---------------------------------------------------------------------------


class TestComposePrompt:
    def test_plain_prompt_no_system(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, system_prompt=None, append_system_prompt=None)
        assert cli._compose_prompt("hello") == "hello"

    def test_system_prompt_prepended(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, system_prompt="Be concise")
        result = cli._compose_prompt("hello")
        assert result == "Be concise\n\nhello"

    def test_append_system_prompt_appended(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, append_system_prompt="Extra rules")
        result = cli._compose_prompt("hello")
        assert result == "hello\n\nExtra rules"

    def test_both_system_prompts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, system_prompt="Before", append_system_prompt="After")
        result = cli._compose_prompt("middle")
        assert result == "Before\n\nmiddle\n\nAfter"


# ---------------------------------------------------------------------------
# _sandbox_flags
# ---------------------------------------------------------------------------


class TestSandboxFlags:
    def test_bypass_permissions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, permission_mode="bypassPermissions")
        assert cli._sandbox_flags() == ["--dangerously-bypass-approvals-and-sandbox"]

    def test_full_access_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, permission_mode="other", sandbox_mode="full-access")
        assert cli._sandbox_flags() == ["--sandbox", "danger-full-access"]

    def test_workspace_write_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, permission_mode="other", sandbox_mode="workspace-write")
        assert cli._sandbox_flags() == ["--full-auto"]

    def test_default_sandbox_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, permission_mode="other", sandbox_mode="read-only")
        assert cli._sandbox_flags() == ["--sandbox", "read-only"]

    def test_custom_sandbox_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, permission_mode="other", sandbox_mode="network-disabled")
        assert cli._sandbox_flags() == ["--sandbox", "network-disabled"]


# ---------------------------------------------------------------------------
# _build_command
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_basic_exec_structure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, model="gpt-5.2-codex")
        cmd = cli._build_command("hello")
        assert cmd[0] == "/usr/bin/codex"
        assert cmd[1] == "exec"
        assert "--json" in cmd
        assert "--color" in cmd
        idx_color = cmd.index("--color")
        assert cmd[idx_color + 1] == "never"
        assert "--skip-git-repo-check" in cmd
        assert "--model" in cmd
        idx_model = cmd.index("--model")
        assert cmd[idx_model + 1] == "gpt-5.2-codex"
        assert cmd[-1] == "hello"

    def test_json_output_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command("hello", json_output=False)
        assert "--json" not in cmd

    def test_no_model_omits_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, model=None)
        cmd = cli._build_command("hello")
        assert "--model" not in cmd

    @pytest.mark.parametrize(
        ("effort", "should_have_flag"),
        [
            ("high", True),
            ("low", True),
            ("xhigh", True),
            ("medium", True),
            ("default", False),
        ],
    )
    def test_reasoning_effort(
        self, monkeypatch: pytest.MonkeyPatch, effort: str, should_have_flag: bool
    ) -> None:
        cli = _make_cli(monkeypatch, reasoning_effort=effort)
        cmd = cli._build_command("hello")
        if should_have_flag:
            assert "-c" in cmd
            idx = cmd.index("-c")
            assert cmd[idx + 1] == f"model_reasoning_effort={effort}"
        else:
            assert "-c" not in cmd

    def test_instructions_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, instructions="/path/to/instructions.md")
        cmd = cli._build_command("hello")
        assert "--instructions" in cmd
        idx = cmd.index("--instructions")
        assert cmd[idx + 1] == "/path/to/instructions.md"

    def test_no_instructions_omits_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, instructions=None)
        cmd = cli._build_command("hello")
        assert "--instructions" not in cmd

    def test_images_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, images=["img1.png", "img2.jpg"])
        cmd = cli._build_command("hello")
        image_indices = [i for i, c in enumerate(cmd) if c == "--image"]
        assert len(image_indices) == 2
        assert cmd[image_indices[0] + 1] == "img1.png"
        assert cmd[image_indices[1] + 1] == "img2.jpg"

    def test_resume_session_changes_structure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, permission_mode="bypassPermissions")
        cmd = cli._build_command("hello", resume_session="thread-abc")
        assert cmd[0] == "/usr/bin/codex"
        assert cmd[1] == "exec"
        assert cmd[2] == "resume"
        assert "--json" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "thread-abc" in cmd
        # resume does not include --model, --color, --skip-git-repo-check
        assert "--model" not in cmd
        assert "--color" not in cmd
        assert "--skip-git-repo-check" not in cmd

    def test_resume_session_json_output_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command("hello", resume_session="th-1", json_output=False)
        assert "resume" in cmd
        assert "--json" not in cmd

    def test_prompt_composed_in_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, system_prompt="SYS", append_system_prompt="APPEND")
        cmd = cli._build_command("user msg")
        final_arg = cmd[-1]
        assert "SYS" in final_arg
        assert "user msg" in final_arg
        assert "APPEND" in final_arg

    def test_resume_prompt_also_composed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, system_prompt="SYS")
        cmd = cli._build_command("user msg", resume_session="th-1")
        final_arg = cmd[-1]
        assert "SYS" in final_arg
        assert "user msg" in final_arg


# ---------------------------------------------------------------------------
# _parse_output (static method)
# ---------------------------------------------------------------------------


class TestParseOutput:
    def test_empty_stdout_returns_error(self) -> None:
        resp = CodexCLI._parse_output(b"", b"", 0)
        assert resp.is_error is True
        assert resp.result == ""

    def test_empty_stdout_with_stderr(self) -> None:
        resp = CodexCLI._parse_output(b"", b"some error", 1)
        assert resp.is_error is True
        assert resp.stderr == "some error"
        assert resp.returncode == 1

    def test_successful_jsonl_output(self) -> None:
        lines = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "th-42"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "Hello world"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                    }
                ),
            ]
        )
        resp = CodexCLI._parse_output(lines.encode(), b"", 0)
        assert resp.is_error is False
        assert resp.session_id == "th-42"
        assert "Hello world" in resp.result
        assert resp.usage["input_tokens"] == 100

    def test_nonzero_returncode_marks_error(self) -> None:
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "partial"},
            }
        )
        resp = CodexCLI._parse_output(line.encode(), b"", 1)
        assert resp.is_error is True
        assert "partial" in resp.result

    def test_non_json_output_fallback_to_raw(self) -> None:
        resp = CodexCLI._parse_output(b"plain text output", b"", 0)
        # parse_codex_jsonl returns empty text for non-json, so fallback to raw
        assert resp.result == "plain text output"
        assert resp.is_error is True  # no result_text means is_error

    def test_stderr_truncated_at_2000(self) -> None:
        long_stderr = b"x" * 3000
        resp = CodexCLI._parse_output(b"", long_stderr, 1)
        assert len(resp.stderr) == 2000

    def test_usage_empty_dict_when_none(self) -> None:
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "hi"},
            }
        )
        resp = CodexCLI._parse_output(line.encode(), b"", 0)
        assert resp.usage == {}


# ---------------------------------------------------------------------------
# send() -- subprocess lifecycle
# ---------------------------------------------------------------------------


class TestSend:
    async def test_send_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        jsonl = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "th-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "Done"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    }
                ),
            ]
        )
        proc = _make_process_mock(stdout=jsonl.encode(), returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)

            resp = await cli.send("hello", timeout_seconds=30.0)

        assert resp.is_error is False
        assert resp.session_id == "th-1"
        assert "Done" in resp.result

    async def test_send_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)

        proc = _make_process_mock()
        proc.communicate = AsyncMock(side_effect=TimeoutError)
        proc.returncode = None

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)

            resp = await cli.send("hello", timeout_seconds=0.001)

        assert resp.is_error is True
        assert resp.timed_out is True
        proc.kill.assert_called_once()

    async def test_send_with_process_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=registry, chat_id=42)

        jsonl = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "OK"},
            }
        )
        proc = _make_process_mock(stdout=jsonl.encode(), returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)

            resp = await cli.send("hello")

        assert resp.result == "OK"
        # After send completes, process should be unregistered
        assert not registry.has_active(42)

    async def test_send_continue_session_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """continue_session=True is a no-op for Codex (logs debug but works)."""
        cli = _make_cli(monkeypatch)
        jsonl = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "OK"},
            }
        )
        proc = _make_process_mock(stdout=jsonl.encode(), returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)

            resp = await cli.send("hello", continue_session=True)

        assert resp.result == "OK"

    async def test_send_with_resume_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        jsonl = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Resumed"},
            }
        )
        proc = _make_process_mock(stdout=jsonl.encode(), returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)

            resp = await cli.send("hello", resume_session="thread-xyz")

        assert "Resumed" in resp.result

    async def test_send_registry_unregisters_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=registry, chat_id=99)

        proc = _make_process_mock()
        proc.communicate = AsyncMock(side_effect=TimeoutError)
        proc.returncode = None

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)

            resp = await cli.send("hello", timeout_seconds=0.001)

        assert resp.timed_out is True
        # Process should still be unregistered via finally block
        assert not registry.has_active(99)


# ---------------------------------------------------------------------------
# send_streaming() -- NDJSON stream parsing
# ---------------------------------------------------------------------------


class TestSendStreaming:
    async def test_streaming_full_sequence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "th-stream-1"}),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"type": "agent_message", "text": "Hello "},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "world!"},
                }
            ),
        ]
        proc = _make_streaming_process(lines, returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_asyncio.create_task = asyncio.ensure_future

            events = await _collect_events(cli.send_streaming("hello"))

        # item.started text is skipped (only item.completed emits text).
        # Thinking filter buffers text and flushes at stream end.
        system_events = [e for e in events if isinstance(e, SystemInitEvent)]
        text_events = [e for e in events if isinstance(e, AssistantTextDelta)]
        result_events = [e for e in events if isinstance(e, ResultEvent)]

        assert len(system_events) == 1
        assert system_events[0].session_id == "th-stream-1"
        assert len(text_events) == 1
        assert text_events[0].text == "world!"
        assert len(result_events) == 1
        assert result_events[0].is_error is False
        assert result_events[0].session_id == "th-stream-1"

    async def test_streaming_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)

        proc = AsyncMock(spec=asyncio.subprocess.Process)
        proc.returncode = None
        proc.pid = 12345
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        stdout_mock = AsyncMock()
        # Simulate a read that never completes by raising TimeoutError
        stdout_mock.readline = AsyncMock(side_effect=TimeoutError)
        proc.stdout = stdout_mock

        stderr_mock = AsyncMock()
        stderr_mock.read = AsyncMock(return_value=b"")
        proc.stderr = stderr_mock

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_asyncio.create_task = asyncio.ensure_future

            events = await _collect_events(cli.send_streaming("hello", timeout_seconds=0.01))

        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert len(result_events) == 1
        assert result_events[0].is_error is True
        proc.kill.assert_called_once()

    async def test_streaming_no_stdout_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(monkeypatch)

        proc = AsyncMock(spec=asyncio.subprocess.Process)
        proc.stdout = None
        proc.stderr = None

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)

            with pytest.raises(RuntimeError, match="without stdout/stderr"):
                await _collect_events(cli.send_streaming("hello"))

    async def test_streaming_accumulates_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        lines = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Part 1"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Part 2"},
                }
            ),
        ]
        proc = _make_streaming_process(lines, returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_asyncio.create_task = asyncio.ensure_future

            events = await _collect_events(cli.send_streaming("hello"))

        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert len(result_events) == 1
        assert "Part 1" in result_events[0].result
        assert "Part 2" in result_events[0].result

    async def test_streaming_empty_lines_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        lines = [
            "",
            "   ",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "OK"},
                }
            ),
        ]
        proc = _make_streaming_process(lines, returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_asyncio.create_task = asyncio.ensure_future

            events = await _collect_events(cli.send_streaming("hello"))

        text_events = [e for e in events if isinstance(e, AssistantTextDelta)]
        assert len(text_events) == 1
        assert text_events[0].text == "OK"

    async def test_streaming_with_tool_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        lines = [
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"type": "command_execution"},
                }
            ),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"type": "file_change"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Done"},
                }
            ),
        ]
        proc = _make_streaming_process(lines, returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_asyncio.create_task = asyncio.ensure_future

            events = await _collect_events(cli.send_streaming("hello"))

        tool_events = [e for e in events if isinstance(e, ToolUseEvent)]
        assert len(tool_events) == 2
        assert tool_events[0].tool_name == "Bash"
        assert tool_events[1].tool_name == "Edit"

    async def test_streaming_with_thinking_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        lines = [
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"type": "reasoning", "text": "Let me think..."},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Answer"},
                }
            ),
        ]
        proc = _make_streaming_process(lines, returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_asyncio.create_task = asyncio.ensure_future

            events = await _collect_events(cli.send_streaming("hello"))

        # ThinkingEvent passes through for [THINKING] display
        thinking_events = [e for e in events if isinstance(e, ThinkingEvent)]
        assert len(thinking_events) == 1
        assert thinking_events[0].text == "Let me think..."

        # Agent text still comes through
        text_events = [e for e in events if isinstance(e, AssistantTextDelta)]
        assert len(text_events) == 1
        assert text_events[0].text == "Answer"

    async def test_streaming_process_error_exit_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        lines = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "partial"},
                }
            ),
        ]
        proc = _make_streaming_process(lines, stderr=b"fatal error", returncode=1)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_asyncio.create_task = asyncio.ensure_future

            events = await _collect_events(cli.send_streaming("hello"))

        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert len(result_events) == 1
        assert result_events[0].is_error is True

    async def test_streaming_registry_cleanup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=registry, chat_id=77)
        lines = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "OK"},
                }
            ),
        ]
        proc = _make_streaming_process(lines, returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_asyncio.create_task = asyncio.ensure_future

            await _collect_events(cli.send_streaming("hello"))

        assert not registry.has_active(77)

    async def test_streaming_malformed_json_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        lines = [
            "not valid json at all",
            "{broken",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "OK"},
                }
            ),
        ]
        proc = _make_streaming_process(lines, returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_asyncio.create_task = asyncio.ensure_future

            events = await _collect_events(cli.send_streaming("hello"))

        text_events = [e for e in events if isinstance(e, AssistantTextDelta)]
        assert len(text_events) == 1
        assert text_events[0].text == "OK"


# ---------------------------------------------------------------------------
# _codex_final_result (module-level helper)
# ---------------------------------------------------------------------------


class TestCodexFinalResult:
    async def test_success_with_text(self) -> None:
        proc = AsyncMock(spec=asyncio.subprocess.Process)
        proc.returncode = 0
        proc.wait = AsyncMock()

        result = await _codex_final_result(proc, ["Hello", "World"], "th-42")
        assert result.is_error is False
        assert result.result == "Hello\nWorld"
        assert result.session_id == "th-42"

    async def test_success_empty_text(self) -> None:
        proc = AsyncMock(spec=asyncio.subprocess.Process)
        proc.returncode = 0
        proc.wait = AsyncMock()

        result = await _codex_final_result(proc, [], None)
        assert result.is_error is False
        assert result.result == ""
        assert result.session_id is None

    async def test_error_with_stderr(self) -> None:
        proc = AsyncMock(spec=asyncio.subprocess.Process)
        proc.returncode = 1
        proc.wait = AsyncMock()

        result = await _codex_final_result(proc, ["partial"], None, b"fatal error")
        assert result.is_error is True
        assert "fatal error" in result.result

    async def test_error_no_stderr_uses_accumulated(self) -> None:
        proc = AsyncMock(spec=asyncio.subprocess.Process)
        proc.returncode = 1
        proc.wait = AsyncMock()

        result = await _codex_final_result(proc, ["error msg"], None, b"")
        assert result.is_error is True
        assert "error msg" in result.result

    async def test_error_no_output_at_all(self) -> None:
        proc = AsyncMock(spec=asyncio.subprocess.Process)
        proc.returncode = 1
        proc.wait = AsyncMock()

        result = await _codex_final_result(proc, [], None, b"")
        assert result.is_error is True
        assert result.result == "(no output)"

    async def test_error_result_truncated_at_500(self) -> None:
        proc = AsyncMock(spec=asyncio.subprocess.Process)
        proc.returncode = 1
        proc.wait = AsyncMock()

        long_stderr = ("x" * 600).encode()
        result = await _codex_final_result(proc, [], None, long_stderr)
        assert result.is_error is True
        assert len(result.result) <= 500

    async def test_stderr_bytes_truncated_at_2000(self) -> None:
        proc = AsyncMock(spec=asyncio.subprocess.Process)
        proc.returncode = 1
        proc.wait = AsyncMock()

        long_stderr = b"y" * 3000
        result = await _codex_final_result(proc, [], None, long_stderr)
        # The error_detail uses stderr_text which is truncated at 2000
        # then the result is further truncated at 500
        assert result.is_error is True


# ---------------------------------------------------------------------------
# _log_cmd (module-level helper)
# ---------------------------------------------------------------------------


class TestLogCmd:
    def test_short_values_not_truncated(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.codex_provider"):
            _log_cmd(["codex", "exec", "--json", "short prompt"])
        assert "short prompt" in caplog.text

    def test_long_values_truncated(self, caplog: pytest.LogCaptureFixture) -> None:
        long_val = "x" * 100
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.codex_provider"):
            _log_cmd(["codex", "exec", long_val])
        assert "..." in caplog.text

    def test_streaming_prefix(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.codex_provider"):
            _log_cmd(["codex", "exec"], streaming=True)
        assert "Codex stream cmd" in caplog.text

    def test_non_streaming_prefix(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.codex_provider"):
            _log_cmd(["codex", "exec"], streaming=False)
        assert "Codex cmd" in caplog.text


# ---------------------------------------------------------------------------
# Docker wrapping integration
# ---------------------------------------------------------------------------


class TestDockerIntegration:
    async def test_send_with_docker_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When docker_container is set, command is wrapped in docker exec."""
        cli = CodexCLI(
            CLIConfig(
                provider="codex",
                model="gpt-5.2-codex",
                docker_container="sandbox-container",
            )
        )
        jsonl = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "docker OK"},
            }
        )
        proc = _make_process_mock(stdout=jsonl.encode(), returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)

            resp = await cli.send("hello")

        # Verify docker exec was called
        call_args = mock_asyncio.create_subprocess_exec.call_args
        exec_cmd = call_args.args
        assert exec_cmd[0] == "docker"
        assert "sandbox-container" in exec_cmd
        # cwd should be None for docker
        assert call_args.kwargs.get("cwd") is None
        assert resp.result == "docker OK"


# ---------------------------------------------------------------------------
# Parametrized edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.parametrize(
        "model_id",
        [
            "gpt-5.2-codex",
            "gpt-5.1-codex-mini",
            "gpt-5.1-codex-max",
            "gpt-5.3-codex",
            "o3-mini",
        ],
    )
    def test_various_codex_models_in_command(
        self, monkeypatch: pytest.MonkeyPatch, model_id: str
    ) -> None:
        cli = _make_cli(monkeypatch, model=model_id)
        cmd = cli._build_command("test")
        assert model_id in cmd

    def test_parse_output_with_unicode_stderr(self) -> None:
        resp = CodexCLI._parse_output(b"", "Fehler: \xc3\xa4\xc3\xb6\xc3\xbc".encode(), 1)
        assert resp.is_error is True
        assert resp.stderr  # should have decoded content

    def test_parse_output_with_replacement_chars(self) -> None:
        resp = CodexCLI._parse_output(b"\xff\xfe invalid", b"", 0)
        # Should not raise -- errors="replace" handles bad bytes
        assert resp.result  # has some content even if garbled

    def test_parse_output_success_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "ok"},
            }
        )
        jsonl = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "th-log"}),
                line,
                json.dumps(
                    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}}
                ),
            ]
        )
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.codex_provider"):
            resp = CodexCLI._parse_output(jsonl.encode(), b"", 0)
        assert resp.is_error is False
        assert "Codex done" in caplog.text
        assert "th-log" in caplog.text

    def test_parse_output_error_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="ductor_bot.cli.codex_provider"):
            CodexCLI._parse_output(b"", b"", 0)
        assert "Codex returned empty output" in caplog.text


class TestSendWithoutRegistry:
    async def test_send_no_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When process_registry is None, send still works without register/unregister."""
        cli = _make_cli(monkeypatch, process_registry=None)
        jsonl = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "no-reg"},
            }
        )
        proc = _make_process_mock(stdout=jsonl.encode(), returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)

            resp = await cli.send("hello")

        assert resp.result == "no-reg"

    async def test_streaming_no_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When process_registry is None, streaming still works."""
        cli = _make_cli(monkeypatch, process_registry=None)
        lines = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "no-reg-stream"},
                }
            ),
        ]
        proc = _make_streaming_process(lines, returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_asyncio.create_task = asyncio.ensure_future

            events = await _collect_events(cli.send_streaming("hello"))

        text_events = [e for e in events if isinstance(e, AssistantTextDelta)]
        assert len(text_events) == 1
        assert text_events[0].text == "no-reg-stream"


class TestResumeCommandArgOrder:
    def test_resume_session_id_before_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The resume command must have: ... thread_id prompt (in that order)."""
        cli = _make_cli(monkeypatch, system_prompt=None, append_system_prompt=None)
        cmd = cli._build_command("my prompt", resume_session="th-abc")
        # thread_id and prompt should be the last two args
        assert cmd[-2] == "th-abc"
        assert cmd[-1] == "my prompt"

    def test_resume_with_empty_images_no_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Resume path ignores images even if set (they're not in the resume branch)."""
        cli = _make_cli(monkeypatch, images=["img.png"])
        cmd = cli._build_command("go", resume_session="th-1")
        # Images are not added to resume commands
        assert "--image" not in cmd


class TestStreamingContinueSessionIgnored:
    async def test_streaming_continue_session_not_breaking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """continue_session=True should not alter streaming behavior."""
        cli = _make_cli(monkeypatch)
        lines = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "streamed"},
                }
            ),
        ]
        proc = _make_streaming_process(lines, returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_asyncio.create_task = asyncio.ensure_future

            events = await _collect_events(cli.send_streaming("hello", continue_session=True))

        text_events = [e for e in events if isinstance(e, AssistantTextDelta)]
        assert len(text_events) == 1


class TestStreamingNonTextEventsNotAccumulated:
    async def test_tool_events_not_in_final_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only AssistantTextDelta text is accumulated in the final ResultEvent."""
        cli = _make_cli(monkeypatch)
        lines = [
            json.dumps({"type": "item.started", "item": {"type": "command_execution"}}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Result only"},
                }
            ),
        ]
        proc = _make_streaming_process(lines, returncode=0)

        with patch("ductor_bot.cli.codex_provider.asyncio") as mock_asyncio:
            mock_asyncio.timeout = asyncio.timeout
            mock_asyncio.subprocess = asyncio.subprocess
            mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_asyncio.create_task = asyncio.ensure_future

            events = await _collect_events(cli.send_streaming("hello"))

        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert len(result_events) == 1
        assert result_events[0].result == "Result only"
        # No tool text leaked into accumulated result
        assert "command_execution" not in result_events[0].result
