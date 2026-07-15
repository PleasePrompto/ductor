"""Unit tests for Grok Build provider event parsing and command building."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.factory import create_cli
from ductor_bot.cli.grok_events import parse_grok_json, parse_grok_stream_line
from ductor_bot.cli.grok_provider import GrokCLI, _parse_response
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ductor_bot.config import ModelRegistry


class TestGrokEvents:
    def test_parse_json_happy_path(self) -> None:
        raw = json.dumps(
            {
                "text": "hello from grok",
                "stopReason": "EndTurn",
                "sessionId": "sid-1",
                "usage": {"input_tokens": 10, "output_tokens": 3},
                "num_turns": 1,
                "modelUsage": {"grok-4.5": {"inputTokens": 10}},
            }
        )
        text, session_id, usage, model_usage, turns, is_error = parse_grok_json(raw)
        assert text == "hello from grok"
        assert session_id == "sid-1"
        assert usage["input_tokens"] == 10
        assert model_usage["grok-4.5"]["inputTokens"] == 10
        assert turns == 1
        assert is_error is False

    def test_parse_stream_thought_text_end(self) -> None:
        events = []
        for line in (
            '{"type":"thought","data":"plan"}',
            '{"type":"text","data":"hi"}',
            '{"type":"text","data":"!"}',
            '{"type":"end","stopReason":"EndTurn","sessionId":"s2","usage":{"input_tokens":1},"num_turns":1}',
        ):
            events.extend(parse_grok_stream_line(line))
        assert isinstance(events[0], ThinkingEvent)
        assert events[0].text == "plan"
        assert isinstance(events[1], AssistantTextDelta)
        assert events[1].text == "hi"
        assert isinstance(events[2], AssistantTextDelta)
        assert events[2].text == "!"
        assert isinstance(events[3], ResultEvent)
        assert events[3].session_id == "s2"
        assert events[3].is_error is False

    def test_parse_stream_tool_use(self) -> None:
        events = parse_grok_stream_line(
            '{"type":"tool_use","name":"Shell","id":"t1","input":{"command":"ls"}}'
        )
        assert len(events) == 1
        assert isinstance(events[0], ToolUseEvent)
        assert events[0].tool_name == "Shell"
        assert events[0].parameters == {"command": "ls"}


class TestGrokProvider:
    def test_find_cli_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.grok_provider.which", lambda _: None)
        with pytest.raises(FileNotFoundError, match="grok CLI not found"):
            GrokCLI(CLIConfig(provider="grok"))

    def test_build_command_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.grok_provider.which", lambda _: "/usr/bin/grok")
        cli = GrokCLI(
            CLIConfig(
                provider="grok",
                model="grok-4.5",
                permission_mode="bypassPermissions",
                reasoning_effort="high",
                system_prompt="SYS",
                append_system_prompt="RULES",
                max_turns=7,
            )
        )
        cmd = cli._build_command("hello world")
        assert cmd[0] == "/usr/bin/grok"
        assert "--output-format" in cmd and "json" in cmd
        assert cmd[cmd.index("-p") + 1] == "hello world"
        assert "--permission-mode" in cmd and "bypassPermissions" in cmd
        assert "--always-approve" in cmd
        assert cmd[cmd.index("--model") + 1] == "grok-4.5"
        assert cmd[cmd.index("--reasoning-effort") + 1] == "high"
        assert cmd[cmd.index("--system-prompt-override") + 1] == "SYS"
        assert cmd[cmd.index("--rules") + 1] == "RULES"
        assert cmd[cmd.index("--max-turns") + 1] == "7"

    def test_build_command_resume_and_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.grok_provider.which", lambda _: "/usr/bin/grok")
        cli = GrokCLI(CLIConfig(provider="grok", model="grok-4.5"))
        cmd = cli._build_command("follow up", resume_session="abc", output_format="streaming-json")
        assert "--output-format" in cmd and "streaming-json" in cmd
        assert cmd[cmd.index("--resume") + 1] == "abc"

    def test_long_prompt_uses_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.grok_provider.which", lambda _: "/usr/bin/grok")
        monkeypatch.setattr("ductor_bot.cli.grok_provider._IS_WINDOWS", False)
        cli = GrokCLI(CLIConfig(provider="grok"))
        long_prompt = "x" * 30_000
        cmd = cli._build_command(long_prompt)
        assert "--prompt-file" in cmd
        path = Path(cmd[cmd.index("--prompt-file") + 1])
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == long_prompt
        cli._cleanup_prompt_files()
        assert not path.exists()

    def test_parse_response(self) -> None:
        payload = {
            "text": "ok",
            "sessionId": "s9",
            "usage": {"input_tokens": 2, "output_tokens": 1},
            "num_turns": 1,
            "stopReason": "EndTurn",
        }
        resp = _parse_response(json.dumps(payload).encode(), b"", 0)
        assert resp.result == "ok"
        assert resp.session_id == "s9"
        assert resp.is_error is False
        assert resp.total_tokens == 3


class TestFactoryAndRegistry:
    def test_factory_returns_grok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.grok_provider.which", lambda _: "/usr/bin/grok")
        cli = create_cli(CLIConfig(provider="grok", model="grok-4.5"))
        assert isinstance(cli, GrokCLI)

    def test_model_registry_routes_grok(self) -> None:
        assert ModelRegistry.provider_for("grok-4.5") == "grok"
        assert ModelRegistry.provider_for("grok-composer-2.5-fast") == "grok"
        assert ModelRegistry.provider_for("grok-custom-future") == "grok"
        assert ModelRegistry.provider_for("opus") == "claude"
