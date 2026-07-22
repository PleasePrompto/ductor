"""Tests for GeminiCLI provider: command building, send, streaming."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.cli.base import CLIConfig

if TYPE_CHECKING:
    import pytest
from ductor_bot.cli.base import host_path_to_container
from ductor_bot.cli.gemini_provider import GeminiCLI, _log_cmd, _parse_response
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cli(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> GeminiCLI:
    monkeypatch.setattr("ductor_bot.cli.gemini_provider.find_gemini_cli", lambda: "/usr/bin/gemini")
    monkeypatch.setattr("ductor_bot.cli.gemini_provider.find_gemini_cli_js", lambda: None)
    return GeminiCLI(
        CLIConfig(
            provider="gemini",
            model=overrides.pop("model", "gemini-2.5-pro"),
            **overrides,
        )
    )


def _make_process_mock(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> AsyncMock:
    proc = AsyncMock(spec=asyncio.subprocess.Process)
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 12345
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    # stdin mock for prompt feeding
    stdin_mock = MagicMock()
    stdin_mock.write = MagicMock()
    stdin_mock.drain = AsyncMock()
    stdin_mock.close = MagicMock()
    proc.stdin = stdin_mock
    return proc


def _make_streaming_process(
    lines: list[str],
    stderr: bytes = b"",
    returncode: int = 0,
) -> AsyncMock:
    proc = AsyncMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode
    proc.pid = 12345
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    encoded_lines = [line.encode() + b"\n" for line in lines] + [b""]
    stdout_mock = AsyncMock()
    stdout_mock.readline = AsyncMock(side_effect=encoded_lines)
    proc.stdout = stdout_mock

    stderr_mock = AsyncMock()
    stderr_mock.read = AsyncMock(return_value=stderr)
    proc.stderr = stderr_mock

    stdin_mock = MagicMock()
    stdin_mock.write = MagicMock()
    stdin_mock.drain = AsyncMock()
    stdin_mock.close = MagicMock()
    proc.stdin = stdin_mock

    return proc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_basic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command()
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--model" in cmd
        assert "gemini-2.5-pro" in cmd

    def test_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command(streaming=True)
        assert "stream-json" in cmd

    def test_bypass_permissions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, permission_mode="bypassPermissions")
        cmd = cli._build_command()
        assert "--approval-mode" in cmd
        assert "yolo" in cmd

    def test_resume_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command(resume_session="abc-123")
        assert "--resume" in cmd
        assert "abc-123" in cmd

    def test_continue_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command(continue_session=True)
        assert "--resume" in cmd
        assert "latest" in cmd

    def test_cli_parameters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, cli_parameters=["--extra", "flag"])
        cmd = cli._build_command()
        assert "--extra" in cmd
        assert "flag" in cmd

    def test_uses_stdin_instead_of_empty_prompt_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gemini should read from stdin instead of sending an empty ``-p`` prompt."""
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command()
        assert "-p" not in cmd
        assert "--prompt" not in cmd


class TestPrepareEnv:
    def test_prepends_cli_parent_when_cli_path_is_absolute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "ductor_bot.cli.gemini_provider.find_gemini_cli",
            lambda: "/opt/node/v22.0.0/bin/gemini",
        )
        monkeypatch.setattr(
            "ductor_bot.cli.gemini_provider.find_gemini_cli_js",
            lambda: "/opt/node/v22.0.0/lib/node_modules/@google/gemini-cli/dist/index.js",
        )
        cli = GeminiCLI(CLIConfig(provider="gemini", model="gemini-2.5-pro"))

        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=False):
            env = cli._prepare_env()

        assert env["PATH"].split(os.pathsep)[0] == "/opt/node/v22.0.0/bin"

    def test_host_to_container_path_normalizes_windows_separators(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_paths = type("P", (), {"ductor_home": Path(r"C:\Users\ZOZN109\.ductor")})()
        monkeypatch.setattr(
            "ductor_bot.workspace.paths.resolve_paths",
            lambda: fake_paths,
        )

        result = host_path_to_container(r"C:\Users\ZOZN109\.ductor\tmp\gemini_system_abc.md")

        assert result == "/ductor/tmp/gemini_system_abc.md"

    def test_injects_config_api_key_for_gemini_api_key_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(monkeypatch, gemini_api_key="cfg-key-123")
        gemini_home = tmp_path / "gemini-home"
        settings = gemini_home / ".gemini" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"security":{"auth":{"selectedType":"gemini-api-key"}}}')

        with patch.dict(
            "os.environ",
            {
                "GEMINI_CLI_HOME": str(gemini_home),
                "GEMINI_API_KEY": "",
                "GOOGLE_GENAI_USE_GCA": "",
                "GOOGLE_GENAI_USE_VERTEXAI": "",
            },
            clear=False,
        ):
            env = cli._prepare_env()

        assert env["GEMINI_API_KEY"] == "cfg-key-123"

    def test_does_not_inject_config_api_key_for_oauth_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(monkeypatch, gemini_api_key="cfg-key-123")
        gemini_home = tmp_path / "gemini-home"
        settings = gemini_home / ".gemini" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"security":{"auth":{"selectedType":"oauth-personal"}}}')

        with patch.dict(
            "os.environ",
            {
                "GEMINI_CLI_HOME": str(gemini_home),
                "GEMINI_API_KEY": "",
                "GOOGLE_GENAI_USE_GCA": "",
                "GOOGLE_GENAI_USE_VERTEXAI": "",
            },
            clear=False,
        ):
            env = cli._prepare_env()

        assert "GEMINI_API_KEY" not in env or env["GEMINI_API_KEY"] == ""

    def test_injects_when_env_key_is_null_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(monkeypatch, gemini_api_key="cfg-key-123")
        gemini_home = tmp_path / "gemini-home"
        settings = gemini_home / ".gemini" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"security":{"auth":{"selectedType":"gemini-api-key"}}}')

        with patch.dict(
            "os.environ",
            {
                "GEMINI_CLI_HOME": str(gemini_home),
                "GEMINI_API_KEY": "null",
                "GOOGLE_GENAI_USE_GCA": "",
                "GOOGLE_GENAI_USE_VERTEXAI": "",
            },
            clear=False,
        ):
            env = cli._prepare_env()

        assert env["GEMINI_API_KEY"] == "cfg-key-123"

    def test_does_not_inject_null_string_config_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(monkeypatch, gemini_api_key="null")
        gemini_home = tmp_path / "gemini-home"
        settings = gemini_home / ".gemini" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"security":{"auth":{"selectedType":"gemini-api-key"}}}')

        with patch.dict(
            "os.environ",
            {
                "GEMINI_CLI_HOME": str(gemini_home),
                "GEMINI_API_KEY": "",
                "GOOGLE_GENAI_USE_GCA": "",
                "GOOGLE_GENAI_USE_VERTEXAI": "",
            },
            clear=False,
        ):
            env = cli._prepare_env()

        assert "GEMINI_API_KEY" not in env or env["GEMINI_API_KEY"] == ""


class TestSend:
    async def test_send_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        response_data = json.dumps({"response": "Hello!", "session_id": "sid-1"})
        proc = _make_process_mock(stdout=response_data.encode(), returncode=0)

        with patch(
            "ductor_bot.cli.gemini_provider.asyncio.create_subprocess_exec", return_value=proc
        ):
            result = await cli.send("Hi")

        assert result.result == "Hello!"
        assert result.session_id == "sid-1"
        assert not result.is_error

    async def test_send_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _make_process_mock()
        # First call times out, second (after kill) returns empty
        proc.communicate.side_effect = [TimeoutError(), (b"", b"")]

        with patch(
            "ductor_bot.cli.gemini_provider.asyncio.create_subprocess_exec", return_value=proc
        ):
            result = await cli.send("Hi", timeout_seconds=0.01)

        assert result.is_error
        assert result.timed_out

    async def test_send_with_process_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=reg, chat_id=42)
        response_data = json.dumps({"response": "OK"})
        proc = _make_process_mock(stdout=response_data.encode(), returncode=0)

        with patch(
            "ductor_bot.cli.gemini_provider.asyncio.create_subprocess_exec", return_value=proc
        ):
            result = await cli.send("test")

        assert not result.is_error
        assert not reg.has_active(42)

    async def test_send_uses_timeout_controller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        response_data = json.dumps({"response": "Hello!", "session_id": "sid-1"})
        proc = _make_process_mock(stdout=response_data.encode(), returncode=0)

        timeout_controller = MagicMock()

        async def _run_with_timeout(coro: Awaitable[tuple[bytes, bytes]]) -> tuple[bytes, bytes]:
            return await coro

        timeout_controller.run_with_timeout = AsyncMock(side_effect=_run_with_timeout)

        with patch(
            "ductor_bot.cli.gemini_provider.asyncio.create_subprocess_exec", return_value=proc
        ):
            result = await cli.send("Hi", timeout_controller=timeout_controller)

        timeout_controller.run_with_timeout.assert_awaited_once()
        assert result.result == "Hello!"
        assert result.session_id == "sid-1"


class TestSendStreaming:
    async def test_full_sequence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        lines = [
            json.dumps({"type": "message", "role": "model", "content": "Hello"}),
            json.dumps(
                {
                    "type": "result",
                    "result": "Done",
                    "stats": {"input_tokens": 10, "output_tokens": 5},
                }
            ),
        ]
        proc = _make_streaming_process(lines)

        with patch(
            "ductor_bot.cli.gemini_provider.asyncio.create_subprocess_exec", return_value=proc
        ):
            events = [event async for event in cli.send_streaming("Hi")]

        text_events = [e for e in events if isinstance(e, AssistantTextDelta)]
        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert len(text_events) >= 1
        assert len(result_events) == 1

    async def test_streaming_abort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=reg, chat_id=99)

        lines = [
            json.dumps({"type": "message", "role": "model", "content": "First"}),
            json.dumps({"type": "message", "role": "model", "content": "Second"}),
        ]
        proc = _make_streaming_process(lines)

        # Set abort flag via kill_all (the public API for aborting)
        # kill_all with no registered processes still sets the abort flag
        await reg.kill_all(99)

        with patch(
            "ductor_bot.cli.gemini_provider.asyncio.create_subprocess_exec", return_value=proc
        ):
            events = [event async for event in cli.send_streaming("Hi")]

        # Should have stopped early due to abort
        assert len(events) <= 1

    async def test_streaming_topic_abort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=reg, chat_id=99, topic_id=42)

        lines = [
            json.dumps({"type": "message", "role": "model", "content": "First"}),
            json.dumps({"type": "message", "role": "model", "content": "Second"}),
        ]
        proc = _make_streaming_process(lines)

        # Topic-scoped /stop marks (chat_id, topic_id); set directly because
        # kill_by_chat_topic only marks when live processes are registered.
        reg._aborted_topics.add((99, 42))

        with patch(
            "ductor_bot.cli.gemini_provider.asyncio.create_subprocess_exec", return_value=proc
        ):
            events = [event async for event in cli.send_streaming("Hi")]

        # Should have stopped early due to the topic abort
        assert len(events) <= 1

    async def test_streaming_other_topic_abort_keeps_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=reg, chat_id=99, topic_id=42)

        lines = [
            json.dumps({"type": "message", "role": "model", "content": "Hello"}),
            json.dumps(
                {
                    "type": "result",
                    "result": "Done",
                    "stats": {"input_tokens": 10, "output_tokens": 5},
                }
            ),
        ]
        proc = _make_streaming_process(lines)

        # A /stop in another topic must not abort this topic's stream
        reg._aborted_topics.add((99, 7))

        with patch(
            "ductor_bot.cli.gemini_provider.asyncio.create_subprocess_exec", return_value=proc
        ):
            events = [event async for event in cli.send_streaming("Hi")]

        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert len(result_events) == 1

    async def test_streaming_nonzero_without_result_emits_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(monkeypatch)
        lines = [json.dumps({"type": "message", "role": "model", "content": "partial"})]
        proc = _make_streaming_process(lines, stderr=b"boom", returncode=1)

        with patch(
            "ductor_bot.cli.gemini_provider.asyncio.create_subprocess_exec", return_value=proc
        ):
            events = [event async for event in cli.send_streaming("Hi")]

        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert len(result_events) == 1
        assert result_events[0].is_error is True
        assert "boom" in result_events[0].result

    async def test_streaming_uses_timeout_controller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        lines = [
            json.dumps({"type": "message", "role": "model", "content": "one"}),
            json.dumps({"type": "result", "result": "done"}),
        ]
        proc = _make_streaming_process(lines)
        timeout_controller = MagicMock()
        timeout_controller.timeout_seconds = 10.0
        timeout_controller.activity_extension_seconds = 2.0
        timeout_controller.start_warning_loop = MagicMock(return_value=None)
        timeout_controller.try_extend = MagicMock(return_value=False)

        with patch(
            "ductor_bot.cli.gemini_provider.asyncio.create_subprocess_exec", return_value=proc
        ):
            events = [
                event
                async for event in cli.send_streaming("Hi", timeout_controller=timeout_controller)
            ]

        assert any(isinstance(e, ResultEvent) for e in events)
        timeout_controller.begin.assert_called_once()
        timeout_controller.start_warning_loop.assert_called_once()
        assert timeout_controller.record_activity.call_count >= 1


class TestParseResponse:
    def test_json_response(self) -> None:
        data = json.dumps({"response": "Result text", "session_id": "s1"})
        resp = _parse_response(data.encode(), b"", 0)
        assert resp.result == "Result text"
        assert resp.session_id == "s1"
        assert not resp.is_error

    def test_empty_stdout(self) -> None:
        resp = _parse_response(b"", b"", 0)
        assert resp.is_error

    def test_non_json_fallback(self) -> None:
        resp = _parse_response(b"raw text output", b"", 0)
        assert resp.result == "raw text output"

    def test_error_returncode(self) -> None:
        data = json.dumps({"response": "error"})
        resp = _parse_response(data.encode(), b"", 1)
        assert resp.is_error

    def test_error_status_in_json(self) -> None:
        data = json.dumps({"status": "error", "error": {"message": "Quota exceeded"}})
        resp = _parse_response(data.encode(), b"", 0)
        assert resp.is_error
        assert resp.result == "Quota exceeded"


class TestLogCmd:
    def test_truncates_long_args(self) -> None:
        cmd = ["gemini", "--prompt", "x" * 200]
        # Should not raise
        _log_cmd(cmd)

    def test_streaming_label(self) -> None:
        _log_cmd(["gemini", "--output-format", "stream-json"], streaming=True)
