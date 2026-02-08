"""Tests for the ClaudeCodeCLI provider -- send(), send_streaming(), edge cases."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.claude_provider import (
    ClaudeCodeCLI,
    _add_opt,
    _log_cmd,
    _parse_response,
)
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
    SystemInitEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXEC_PATH = "ductor_bot.cli.claude_provider.asyncio.create_subprocess_exec"


def _make_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: str = "opus",
    docker_container: str = "",
    process_registry: ProcessRegistry | None = None,
    chat_id: int = 1,
    **kwargs: Any,
) -> ClaudeCodeCLI:
    """Create a ClaudeCodeCLI with `which` stubbed out."""
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
    cfg = CLIConfig(
        provider="claude",
        model=model,
        docker_container=docker_container,
        process_registry=process_registry,
        chat_id=chat_id,
        **kwargs,
    )
    return ClaudeCodeCLI(cfg)


def _fake_process(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Build a mock asyncio.subprocess.Process."""
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.returncode = returncode
    proc.pid = 12345
    proc.kill = MagicMock()
    return proc


def _fake_streaming_process(
    lines: list[bytes],
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Build a mock process whose stdout.readline() yields lines then b""."""
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.pid = 12345
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode)

    line_iter = iter([*lines, b""])
    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(side_effect=lambda: next(line_iter))
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=stderr)
    return proc


async def _collect_stream(
    cli: ClaudeCodeCLI,
    prompt: str = "hello",
    **kwargs: Any,
) -> list[StreamEvent]:
    """Exhaust send_streaming() and return all events as a list."""
    return [event async for event in cli.send_streaming(prompt, **kwargs)]


# ---------------------------------------------------------------------------
# __init__ / _find_cli
# ---------------------------------------------------------------------------


class TestInit:
    def test_find_cli_not_found_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: None)
        with pytest.raises(FileNotFoundError, match="claude CLI not found"):
            ClaudeCodeCLI(CLIConfig(provider="claude"))

    def test_docker_container_skips_find_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When docker_container is set, CLI binary = 'claude' without PATH lookup."""
        monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: None)
        cli = ClaudeCodeCLI(CLIConfig(provider="claude", docker_container="my-container"))
        assert cli._cli == "claude"

    def test_working_dir_resolved(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
        cfg = CLIConfig(provider="claude", working_dir=str(tmp_path / "sub" / ".."))
        cli = ClaudeCodeCLI(cfg)
        assert cli._working_dir == tmp_path.resolve()


# ---------------------------------------------------------------------------
# _build_command edge cases not covered by test_providers.py
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_max_budget_usd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, max_budget_usd=2.5)
        cmd = cli._build_command("go")
        assert "--max-budget-usd" in cmd
        idx = cmd.index("--max-budget-usd")
        assert cmd[idx + 1] == "2.5"

    def test_disallowed_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, disallowed_tools=["Bash", "Write"])
        cmd = cli._build_command("go")
        assert "--disallowedTools" in cmd
        idx = cmd.index("--disallowedTools")
        assert cmd[idx + 1] == "Bash"
        assert cmd[idx + 2] == "Write"

    def test_resume_takes_precedence_over_continue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When both resume_session and continue_session are set, --resume wins."""
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command("go", resume_session="sess-1", continue_session=True)
        assert "--resume" in cmd
        assert "--continue" not in cmd

    def test_no_none_values_in_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ensure optional None fields do not produce '--flag None' pairs."""
        monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
        cfg = CLIConfig(provider="claude", model=None, max_turns=None, max_budget_usd=None)
        cli = ClaudeCodeCLI(cfg)
        cmd = cli._build_command("go")
        assert "None" not in cmd

    @pytest.mark.parametrize("model", ["haiku", "sonnet", "opus"])
    def test_model_variants(self, monkeypatch: pytest.MonkeyPatch, model: str) -> None:
        cli = _make_cli(monkeypatch, model=model)
        cmd = cli._build_command("hi")
        idx = cmd.index("--model")
        assert cmd[idx + 1] == model

    def test_prompt_is_always_last(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(
            monkeypatch,
            allowed_tools=["Read"],
            system_prompt="Be nice",
            max_turns=10,
        )
        cmd = cli._build_command("the prompt text")
        assert cmd[-1] == "the prompt text"


class TestBuildCommandStreaming:
    def test_replaces_json_with_stream_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command_streaming("go")
        assert "stream-json" in cmd
        assert "json" not in cmd

    def test_verbose_flag_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command_streaming("go")
        assert "--verbose" in cmd

    def test_verbose_not_duplicated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command_streaming("go")
        assert cmd.count("--verbose") == 1

    def test_json_not_in_command_defensive_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cover the except ValueError branch when 'json' is absent from command."""
        cli = _make_cli(monkeypatch)
        with patch.object(
            cli,
            "_build_command",
            return_value=["/usr/bin/claude", "-p", "--output-format", "text", "hello"],
        ):
            cmd = cli._build_command_streaming("hello")
        assert "text" in cmd
        assert "--verbose" in cmd

    def test_resume_carried_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command_streaming("go", resume_session="sess-7")
        assert "--resume" in cmd
        assert "sess-7" in cmd
        assert "stream-json" in cmd


# ---------------------------------------------------------------------------
# send() -- async execution
# ---------------------------------------------------------------------------


class TestSend:
    async def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        data = {
            "session_id": "sess-1",
            "result": "Done!",
            "is_error": False,
            "total_cost_usd": 0.03,
            "num_turns": 2,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        proc = _fake_process(stdout=json.dumps(data).encode(), returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello")

        assert resp.result == "Done!"
        assert resp.session_id == "sess-1"
        assert resp.is_error is False
        assert resp.total_cost_usd == 0.03
        assert resp.timed_out is False

    async def test_timeout_returns_timed_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_process()
        proc.communicate = AsyncMock(side_effect=TimeoutError)
        proc.wait = AsyncMock()

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello", timeout_seconds=1.0)

        assert resp.timed_out is True
        assert resp.is_error is True
        assert resp.result == ""
        proc.kill.assert_called_once()

    async def test_empty_stdout_is_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_process(stdout=b"", returncode=1)

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello")

        assert resp.is_error is True

    async def test_process_registry_register_unregister(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=registry, chat_id=42)
        data = {"result": "OK", "is_error": False}
        proc = _fake_process(stdout=json.dumps(data).encode())

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello")

        assert resp.result == "OK"
        assert not registry.has_active(42)

    async def test_process_registry_unregister_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=registry, chat_id=42)
        proc = _fake_process()
        proc.communicate = AsyncMock(side_effect=TimeoutError)
        proc.wait = AsyncMock()

        with patch(_EXEC_PATH, return_value=proc):
            await cli.send("hello", timeout_seconds=0.1)

        assert not registry.has_active(42)

    async def test_no_registry_does_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, process_registry=None)
        data = {"result": "OK"}
        proc = _fake_process(stdout=json.dumps(data).encode())

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello")

        assert resp.result == "OK"

    @pytest.mark.parametrize(
        ("resume_session", "continue_session"),
        [
            ("sess-abc", False),
            (None, True),
            (None, False),
        ],
    )
    async def test_session_flags_forwarded(
        self,
        monkeypatch: pytest.MonkeyPatch,
        resume_session: str | None,
        continue_session: bool,
    ) -> None:
        cli = _make_cli(monkeypatch)
        data = {"result": "OK"}
        proc = _fake_process(stdout=json.dumps(data).encode())

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            await cli.send(
                "hello",
                resume_session=resume_session,
                continue_session=continue_session,
            )

        called_cmd = mock_exec.call_args[0]
        if resume_session:
            assert "--resume" in called_cmd
            assert resume_session in called_cmd
        elif continue_session:
            assert "--continue" in called_cmd
        else:
            assert "--resume" not in called_cmd
            assert "--continue" not in called_cmd

    async def test_docker_container_wraps_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, docker_container="sandbox-1", chat_id=55)
        data = {"result": "OK"}
        proc = _fake_process(stdout=json.dumps(data).encode())

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            resp = await cli.send("hello")

        called_cmd = mock_exec.call_args[0]
        assert called_cmd[0] == "docker"
        assert "sandbox-1" in called_cmd
        assert resp.result == "OK"

    async def test_stderr_captured_in_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        data = {"result": "ok", "is_error": False}
        proc = _fake_process(
            stdout=json.dumps(data).encode(),
            stderr=b"some warning",
            returncode=0,
        )

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello")

        assert resp.stderr == "some warning"

    async def test_invalid_json_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_process(stdout=b"not json at all!", returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello")

        assert resp.is_error is True
        assert "not json at all!" in resp.result


# ---------------------------------------------------------------------------
# send_streaming() -- NDJSON stream processing
# ---------------------------------------------------------------------------


class TestSendStreaming:
    async def test_happy_path_yields_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        init_line = (
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}).encode()
            + b"\n"
        )
        assistant_line = (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Hello world"}]},
                }
            ).encode()
            + b"\n"
        )
        result_line = (
            json.dumps(
                {
                    "type": "result",
                    "session_id": "sess-1",
                    "result": "Hello world",
                    "is_error": False,
                    "total_cost_usd": 0.02,
                    "usage": {"input_tokens": 50, "output_tokens": 25},
                }
            ).encode()
            + b"\n"
        )

        proc = _fake_streaming_process([init_line, assistant_line, result_line], returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        assert any(isinstance(e, SystemInitEvent) for e in events)
        assert any(isinstance(e, AssistantTextDelta) and e.text == "Hello world" for e in events)
        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert len(result_events) == 1
        assert result_events[0].session_id == "sess-1"
        assert result_events[0].total_cost_usd == 0.02

    async def test_timeout_yields_error_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([])
        proc.stdout.readline = AsyncMock(side_effect=TimeoutError)
        proc.wait = AsyncMock()

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli, timeout_seconds=0.1)

        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)
        assert events[0].is_error is True
        proc.kill.assert_called_once()

    async def test_nonzero_exit_yields_error_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([], stderr=b"fatal error", returncode=1)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)
        assert events[0].is_error is True
        assert "fatal error" in events[0].result

    async def test_empty_stream_no_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([], returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        assert events == []

    async def test_malformed_json_lines_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        good_line = (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "OK"}]},
                }
            ).encode()
            + b"\n"
        )
        proc = _fake_streaming_process([b"not-json\n", b"  \n", good_line], returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        assert len(events) == 1
        assert isinstance(events[0], AssistantTextDelta)
        assert events[0].text == "OK"

    async def test_process_registry_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=registry, chat_id=99)
        proc = _fake_streaming_process([], returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            await _collect_stream(cli)

        assert not registry.has_active(99)

    async def test_process_registry_cleanup_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=registry, chat_id=99)
        proc = _fake_streaming_process([])
        proc.stdout.readline = AsyncMock(side_effect=TimeoutError)
        proc.wait = AsyncMock()

        with patch(_EXEC_PATH, return_value=proc):
            await _collect_stream(cli, timeout_seconds=0.1)

        assert not registry.has_active(99)

    async def test_missing_pipes_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(monkeypatch)
        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.stdout = None
        proc.stderr = None
        proc.pid = 12345

        with (
            patch(_EXEC_PATH, return_value=proc),
            pytest.raises(RuntimeError, match="without stdout/stderr pipes"),
        ):
            await _collect_stream(cli)

    async def test_streaming_uses_stream_json_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([], returncode=0)

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            await _collect_stream(cli)

        called_cmd = mock_exec.call_args[0]
        assert "stream-json" in called_cmd
        assert "--verbose" in called_cmd

    async def test_stderr_truncated_at_500_chars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        long_stderr = b"X" * 1000
        proc = _fake_streaming_process([], stderr=long_stderr, returncode=1)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert len(result_events) == 1
        assert len(result_events[0].result) == 500

    async def test_streaming_limit_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the 4MB buffer limit is passed to create_subprocess_exec."""
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([], returncode=0)

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            await _collect_stream(cli)

        assert mock_exec.call_args[1]["limit"] == 4 * 1024 * 1024

    async def test_multiple_text_deltas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": f"chunk{i}"}]},
                }
            ).encode()
            + b"\n"
            for i in range(3)
        ]
        proc = _fake_streaming_process(lines, returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        text_events = [e for e in events if isinstance(e, AssistantTextDelta)]
        assert len(text_events) == 3
        assert [e.text for e in text_events] == ["chunk0", "chunk1", "chunk2"]

    async def test_resume_session_in_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([], returncode=0)

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            await _collect_stream(cli, resume_session="sess-42")

        called_cmd = mock_exec.call_args[0]
        assert "--resume" in called_cmd
        assert "sess-42" in called_cmd

    async def test_no_registry_streaming_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(monkeypatch, process_registry=None)
        proc = _fake_streaming_process([], returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        assert events == []

    async def test_zero_exit_with_empty_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A zero exit code with no output should yield nothing (no error event)."""
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([], stderr=b"", returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        assert not any(isinstance(e, ResultEvent) and e.is_error for e in events)


# ---------------------------------------------------------------------------
# _add_opt helper
# ---------------------------------------------------------------------------


class TestAddOpt:
    def test_adds_flag_when_value_present(self) -> None:
        cmd: list[str] = []
        _add_opt(cmd, "--model", "opus")
        assert cmd == ["--model", "opus"]

    def test_skips_when_value_none(self) -> None:
        cmd: list[str] = []
        _add_opt(cmd, "--model", None)
        assert cmd == []

    def test_skips_when_value_empty_string(self) -> None:
        cmd: list[str] = []
        _add_opt(cmd, "--model", "")
        assert cmd == []


# ---------------------------------------------------------------------------
# _log_cmd helper
# ---------------------------------------------------------------------------


class TestLogCmd:
    def test_truncates_long_values_after_flags(self, caplog: pytest.LogCaptureFixture) -> None:
        long_prompt = "x" * 200
        cmd = ["claude", "--system-prompt", long_prompt, "short"]
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.claude_provider"):
            _log_cmd(cmd)
        assert "..." in caplog.text

    def test_does_not_truncate_short_values(self, caplog: pytest.LogCaptureFixture) -> None:
        cmd = ["claude", "--model", "opus", "hello"]
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.claude_provider"):
            _log_cmd(cmd)
        assert "..." not in caplog.text

    def test_streaming_prefix(self, caplog: pytest.LogCaptureFixture) -> None:
        cmd = ["claude", "-p", "hello"]
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.claude_provider"):
            _log_cmd(cmd, streaming=True)
        assert "CLI stream cmd" in caplog.text

    def test_normal_prefix(self, caplog: pytest.LogCaptureFixture) -> None:
        cmd = ["claude", "-p", "hello"]
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.claude_provider"):
            _log_cmd(cmd, streaming=False)
        assert "CLI cmd" in caplog.text


# ---------------------------------------------------------------------------
# _parse_response (additional edge cases beyond test_parse_response.py)
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_stderr_truncated_at_2000_chars(self) -> None:
        long_stderr = b"E" * 5000
        data = {"result": "OK", "is_error": False}
        resp = _parse_response(json.dumps(data).encode(), long_stderr, 0)
        assert len(resp.stderr) == 2000

    def test_returncode_none(self) -> None:
        data = {"result": "OK"}
        resp = _parse_response(json.dumps(data).encode(), b"", None)
        assert resp.returncode is None

    def test_model_usage_camel_case_key(self) -> None:
        data = {
            "result": "OK",
            "modelUsage": {"claude-opus-4-20250514": {"input_tokens": 999}},
        }
        resp = _parse_response(json.dumps(data).encode(), b"", 0)
        assert "claude-opus-4-20250514" in resp.model_usage

    def test_whitespace_only_stdout_is_error(self) -> None:
        resp = _parse_response(b"   \n\t  ", b"", 0)
        assert resp.is_error is True

    def test_usage_defaults_to_empty_dict(self) -> None:
        data = {"result": "OK"}
        resp = _parse_response(json.dumps(data).encode(), b"", 0)
        assert resp.usage == {}
        assert resp.model_usage == {}
        assert resp.total_tokens == 0

    def test_stderr_in_response_object(self) -> None:
        data = {"result": "ok"}
        resp = _parse_response(json.dumps(data).encode(), b"warning text", 0)
        assert resp.stderr == "warning text"

    def test_error_result_has_correct_fields(self) -> None:
        data = {"result": "Something broke", "is_error": True}
        resp = _parse_response(json.dumps(data).encode(), b"", 1)
        assert resp.is_error is True
        assert resp.result == "Something broke"
        assert resp.returncode == 1

    def test_json_with_surrounding_whitespace(self) -> None:
        data = {"result": "trimmed", "is_error": False}
        raw = f"\n  {json.dumps(data)}  \n"
        resp = _parse_response(raw.encode(), b"", 0)
        assert resp.result == "trimmed"
        assert resp.is_error is False

    def test_duration_fields_populated(self) -> None:
        data = {
            "result": "ok",
            "duration_ms": 1234.5,
            "duration_api_ms": 900.0,
        }
        resp = _parse_response(json.dumps(data).encode(), b"", 0)
        assert resp.duration_ms == 1234.5
        assert resp.duration_api_ms == 900.0

    def test_num_turns_captured(self) -> None:
        data = {"result": "ok", "num_turns": 5}
        resp = _parse_response(json.dumps(data).encode(), b"", 0)
        assert resp.num_turns == 5

    def test_empty_stderr_bytes_yields_empty_string(self) -> None:
        data = {"result": "ok"}
        resp = _parse_response(json.dumps(data).encode(), b"", 0)
        assert resp.stderr == ""
