"""Tests for the GeminiCLI provider -- send(), send_streaming(), edge cases."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.gemini_provider import (
    GeminiCLI,
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

_EXEC_PATH = "ductor_bot.cli.gemini_provider.asyncio.create_subprocess_exec"


def _make_cli(
    monkeypatch: MonkeyPatch,
    *,
    model: str = "flash",
    docker_container: str = "",
    process_registry: ProcessRegistry | None = None,
    chat_id: int = 1,
    **kwargs: Any,
) -> GeminiCLI:
    """Create a GeminiCLI with path lookups stubbed out."""
    # Stub find_cli_js to avoid calling npm root -g
    monkeypatch.setattr(
        "ductor_bot.cli.gemini_provider.GeminiCLI._find_cli_js", lambda _: "/path/to/gemini.js"
    )
    # Stub _trust_workspace to avoid FS writes
    monkeypatch.setattr("ductor_bot.cli.gemini_provider.GeminiCLI._trust_workspace", lambda _: None)

    cfg = CLIConfig(
        provider="gemini",
        model=model,
        docker_container=docker_container,
        process_registry=process_registry,
        chat_id=chat_id,
        **kwargs,
    )
    # Patch which since it's used in __init__
    with patch("shutil.which", return_value="/usr/bin/gemini"):
        return GeminiCLI(cfg)


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
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()
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
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()

    line_iter = iter([*lines, b""])
    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(side_effect=lambda: next(line_iter))
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=stderr)
    return proc


async def _collect_stream(
    cli: GeminiCLI,
    prompt: str = "hello",
    **kwargs: Any,
) -> list[StreamEvent]:
    """Exhaust send_streaming() and return all events as a list."""
    return [event async for event in cli.send_streaming(prompt, **kwargs)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInit:
    def test_trust_workspace_called(self, monkeypatch: MonkeyPatch) -> None:
        mock_trust = MagicMock()
        monkeypatch.setattr("ductor_bot.cli.gemini_provider.GeminiCLI._trust_workspace", mock_trust)
        monkeypatch.setattr("ductor_bot.cli.gemini_provider.GeminiCLI._find_cli_js", lambda _: None)

        with patch("shutil.which", return_value="/usr/bin/gemini"):
            GeminiCLI(CLIConfig(provider="gemini"))

        mock_trust.assert_called_once()


class TestBuildCommand:
    def test_basic_command(self, monkeypatch: MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        with patch("shutil.which", return_value="/usr/bin/gemini"):
            cmd = cli._build_command()
        assert "node" in cmd
        assert "/path/to/gemini.js" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd

    def test_streaming_command(self, monkeypatch: MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        with patch("shutil.which", return_value="/usr/bin/gemini"):
            cmd = cli._build_command(streaming=True)
        assert "stream-json" in cmd

    def test_yolo_mode(self, monkeypatch: MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, permission_mode="bypassPermissions")
        with patch("shutil.which", return_value="/usr/bin/gemini"):
            cmd = cli._build_command()
        assert "--approval-mode" in cmd
        assert "yolo" in cmd

    def test_allowed_tools(self, monkeypatch: MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, allowed_tools=["google_web_search", "bash"])
        with patch("shutil.which", return_value="/usr/bin/gemini"):
            cmd = cli._build_command()
        assert "--allowed-tools" in cmd
        assert "google_web_search" in cmd
        assert "bash" in cmd


class TestSend:
    async def test_happy_path(self, monkeypatch: MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        data = {"response": "Hello Nana!", "session_id": "gem-1"}
        proc = _fake_process(stdout=json.dumps(data).encode())

        with (
            patch("shutil.which", return_value="/usr/bin/gemini"),
            patch(_EXEC_PATH, return_value=proc),
        ):
            resp = await cli.send("hi")

        assert resp.result == "Hello Nana!"
        assert resp.session_id == "gem-1"
        # communicate(input=b"hi") was called
        proc.communicate.assert_called_with(input=b"hi")

    async def test_error_exit(self, monkeypatch: MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_process(stdout=b'{"error": "bad request"}', returncode=1)

        with (
            patch("shutil.which", return_value="/usr/bin/gemini"),
            patch(_EXEC_PATH, return_value=proc),
        ):
            resp = await cli.send("hi")

        assert resp.is_error is True


class TestSendStreaming:
    async def test_happy_path(self, monkeypatch: MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        lines = [
            json.dumps({"type": "init", "session_id": "gem-stream"}).encode() + b"\n",
            json.dumps({"type": "message", "role": "assistant", "content": "Live"}).encode()
            + b"\n",
            json.dumps({"type": "result", "status": "success", "response": "Live content"}).encode()
            + b"\n",
        ]
        proc = _fake_streaming_process(lines)

        with (
            patch("shutil.which", return_value="/usr/bin/gemini"),
            patch(_EXEC_PATH, return_value=proc),
        ):
            events = await _collect_stream(cli)

        assert len(events) == 3
        assert isinstance(events[0], SystemInitEvent)
        assert isinstance(events[1], AssistantTextDelta)
        assert isinstance(events[2], ResultEvent)
        assert events[2].result == "Live content"

    async def test_timeout(self, monkeypatch: MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([])
        # Mock timeout in the loop
        with (
            patch("asyncio.timeout", side_effect=asyncio.TimeoutError),
            patch("shutil.which", return_value="/usr/bin/gemini"),
            patch(_EXEC_PATH, return_value=proc),
        ):
            events = await _collect_stream(cli)

        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)
        assert events[0].result == "Timeout"
        assert events[0].is_error is True

    async def test_abort_by_user(self, monkeypatch: MonkeyPatch) -> None:
        reg = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=reg, chat_id=123)
        proc = _fake_streaming_process([])

        # Simulate abort
        reg.register(123, proc, "gemini")
        await reg.kill_all(123)

        with (
            patch("shutil.which", return_value="/usr/bin/gemini"),
            patch(_EXEC_PATH, return_value=proc),
        ):
            events = await _collect_stream(cli)

        assert any(isinstance(e, ResultEvent) and "aborted" in e.result for e in events)


class TestParseResponse:
    def test_parse_usage(self) -> None:
        data = {"response": "ok", "stats": {"input_tokens": 10, "output_tokens": 20}}
        resp = _parse_response(json.dumps(data).encode(), b"", 0)
        assert resp.usage["input_tokens"] == 10
        assert resp.usage["output_tokens"] == 20

    def test_invalid_json_fallback(self) -> None:
        resp = _parse_response(b"plain text", b"", 0)
        assert resp.result == "plain text"

    def test_empty_stdout_is_error(self) -> None:
        resp = _parse_response(b"", b"stderr info", 1)
        assert resp.is_error is True
        assert resp.stderr == "stderr info"
