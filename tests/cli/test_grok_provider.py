"""Unit tests for Grok Build provider event parsing and command building."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.factory import create_cli
from ductor_bot.cli.grok_events import parse_grok_json, parse_grok_stream_line
from ductor_bot.cli.grok_provider import GrokCLI, _parse_response
from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    CompactBoundaryEvent,
    ResultEvent,
    SystemStatusEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import ModelRegistry
from ductor_bot.cron.execution import _build_grok_cmd


class TestGrokEvents:
    def test_parse_json_happy_path(self) -> None:
        raw = json.dumps(
            {
                "text": "hello from grok",
                "stopReason": "EndTurn",
                "sessionId": "sid-1",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 100,
                    "output_tokens": 3,
                    "total_tokens": 113,
                },
                "num_turns": 1,
                "modelUsage": {"grok-4.5": {"inputTokens": 10}},
                "total_cost_usd": 0.0123,
            }
        )
        text, session_id, usage, model_usage, turns, is_error, cost = parse_grok_json(raw)
        assert text == "hello from grok"
        assert session_id == "sid-1"
        assert usage["input_tokens"] == 10
        assert usage["total_tokens"] == 113
        assert model_usage["grok-4.5"]["inputTokens"] == 10
        assert turns == 1
        assert is_error is False
        assert cost == 0.0123

    def test_parse_json_error_envelope(self) -> None:
        raw = json.dumps({"type": "error", "message": "Couldn't start session"})
        text, _sid, _u, _m, _t, is_error, _c = parse_grok_json(raw)
        assert is_error is True
        assert "Couldn't start session" in text

    def test_parse_stream_thought_text_end(self) -> None:
        events = []
        for line in (
            '{"type":"thought","data":"plan"}',
            '{"type":"text","data":"hi"}',
            '{"type":"text","data":"!"}',
            '{"type":"end","stopReason":"EndTurn","sessionId":"s2",'
            '"usage":{"input_tokens":1,"total_tokens":5},"num_turns":1,"total_cost_usd":0.01}',
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
        assert events[3].total_cost_usd == 0.01
        assert events[3].usage["total_tokens"] == 5

    def test_parse_stream_tool_use(self) -> None:
        events = parse_grok_stream_line(
            '{"type":"tool_use","name":"Shell","id":"t1","input":{"command":"ls"}}'
        )
        assert len(events) == 1
        assert isinstance(events[0], ToolUseEvent)
        assert events[0].tool_name == "Shell"
        assert events[0].parameters == {"command": "ls"}

    def test_parse_stream_error(self) -> None:
        events = parse_grok_stream_line('{"type":"error","message":"auth failed","sessionId":"e1"}')
        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)
        assert events[0].is_error is True
        assert events[0].result == "auth failed"
        assert events[0].session_id == "e1"

    def test_parse_stream_auto_compact(self) -> None:
        events = parse_grok_stream_line(
            '{"type":"auto_compact_start","pre_tokens":12000,"trigger":"threshold"}'
        )
        assert len(events) == 1
        assert isinstance(events[0], CompactBoundaryEvent)
        assert events[0].pre_tokens == 12000
        assert "auto_compact" in events[0].trigger or events[0].trigger == "threshold"

    def test_parse_stream_max_turns(self) -> None:
        events = parse_grok_stream_line('{"type":"max_turns_reached"}')
        assert len(events) == 1
        assert isinstance(events[0], SystemStatusEvent)
        assert events[0].status == "max_turns_reached"


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
                allowed_tools=["read_file", "grep"],
                disallowed_tools=["run_terminal_cmd"],
            )
        )
        cmd = cli._build_command("hello world")
        assert cmd[0] == "/usr/bin/grok"
        assert "--output-format" in cmd
        assert "json" in cmd
        assert cmd[cmd.index("-p") + 1] == "hello world"
        assert "--permission-mode" in cmd
        assert "bypassPermissions" in cmd
        assert "--always-approve" in cmd
        assert cmd[cmd.index("--model") + 1] == "grok-4.5"
        assert cmd[cmd.index("--reasoning-effort") + 1] == "high"
        assert cmd[cmd.index("--system-prompt-override") + 1] == "SYS"
        assert cmd[cmd.index("--rules") + 1] == "RULES"
        assert cmd[cmd.index("--max-turns") + 1] == "7"
        assert cmd[cmd.index("--tools") + 1] == "read_file,grep"
        assert cmd[cmd.index("--disallowed-tools") + 1] == "run_terminal_cmd"
        # Must not confuse tool filters with permission allow/deny rules.
        assert "--allow" not in cmd
        assert "--deny" not in cmd

    def test_build_command_resume_and_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.grok_provider.which", lambda _: "/usr/bin/grok")
        cli = GrokCLI(CLIConfig(provider="grok", model="grok-4.5"))
        cmd = cli._build_command("follow up", resume_session="abc", output_format="streaming-json")
        assert "--output-format" in cmd
        assert "streaming-json" in cmd
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

    def test_parse_response_cost_and_total_tokens(self) -> None:
        payload = {
            "text": "ok",
            "sessionId": "s9",
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 900,
                "output_tokens": 50,
                "total_tokens": 1050,
            },
            "num_turns": 1,
            "stopReason": "EndTurn",
            "total_cost_usd": 0.0042,
        }
        resp = _parse_response(json.dumps(payload).encode(), b"", 0)
        assert resp.result == "ok"
        assert resp.session_id == "s9"
        assert resp.is_error is False
        assert resp.total_cost_usd == 0.0042
        assert resp.total_tokens == 1050  # prefers usage.total_tokens


def _grok_task_cfg(**kwargs: Any) -> TaskExecutionConfig:
    base: dict[str, Any] = {
        "provider": "grok",
        "model": "grok-4.5",
        "reasoning_effort": "medium",
        "permission_mode": "bypassPermissions",
        "cli_parameters": [],
        "working_dir": "/tmp",
        "file_access": "all",
    }
    base.update(kwargs)
    return TaskExecutionConfig(**base)


class TestGrokCronCmd:
    def test_short_prompt_uses_p(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "/usr/bin/grok")
        one = _build_grok_cmd(_grok_task_cfg(), "hello")
        assert one is not None
        assert "-p" in one.cmd
        assert one.cmd[one.cmd.index("-p") + 1] == "hello"
        assert one.cleanup_paths == []

    def test_long_prompt_uses_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "/usr/bin/grok")
        long_prompt = "y" * 30_000
        one = _build_grok_cmd(_grok_task_cfg(reasoning_effort=""), long_prompt)
        assert one is not None
        assert "--prompt-file" in one.cmd
        path = Path(one.cmd[one.cmd.index("--prompt-file") + 1])
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == long_prompt
        assert path in one.cleanup_paths
        path.unlink(missing_ok=True)


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

    def test_cli_response_prefers_explicit_total_tokens(self) -> None:
        r = CLIResponse(
            usage={
                "input_tokens": 100,
                "cache_read_input_tokens": 900,
                "output_tokens": 50,
                "total_tokens": 1050,
            }
        )
        assert r.total_tokens == 1050
