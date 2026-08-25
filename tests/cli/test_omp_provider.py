"""Unit tests for Oh My Pi provider event parsing and command building."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.factory import create_cli
from ductor_bot.cli.omp_events import parse_omp_json, parse_omp_stream_line
from ductor_bot.cli.omp_provider import OmpCLI, _parse_response
from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    CompactBoundaryEvent,
    ResultEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ductor_bot.config import ModelRegistry
from ductor_bot.cron.execution import _build_omp_cmd


class TestOmpEvents:
    def test_parse_json_happy_path(self) -> None:
        raw = (
            json.dumps(
                {
                    "type": "session",
                    "id": "sess-123",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "agent_end",
                    "id": "sess-123",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "hello from omp"}],
                            "usage": {"input": 10, "output": 20},
                            "cost": {"total": 0.01},
                        }
                    ],
                }
            )
        )
        text, sid, usage, _mu, _turns, is_err, cost = parse_omp_json(raw)
        assert text == "hello from omp"
        assert sid == "sess-123"
        assert usage["input_tokens"] == 10
        assert cost == 0.01
        assert not is_err

    def test_parse_json_empty(self) -> None:
        text, sid, _u, _mu, _t, is_err, _c = parse_omp_json("")
        assert text == ""
        assert sid is None
        assert is_err

    def test_stream_text_delta(self) -> None:
        line = json.dumps(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "hello"},
            }
        )
        events = parse_omp_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], AssistantTextDelta)
        assert events[0].text == "hello"

    def test_stream_session_init(self) -> None:
        line = json.dumps({"type": "session", "id": "sess-abc"})
        events = parse_omp_stream_line(line)
        assert len(events) == 1
        assert events[0].type == "system"

    def test_stream_tool_execution(self) -> None:
        line = json.dumps(
            {
                "type": "tool_execution_start",
                "toolName": "read",
                "toolCallId": "t1",
                "args": {"path": "a.txt"},
            }
        )
        events = parse_omp_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], ToolUseEvent)
        assert events[0].tool_name == "read"

    def test_stream_thinking_delta(self) -> None:
        line = json.dumps(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_delta", "delta": "hmm"},
            }
        )
        events = parse_omp_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], ThinkingEvent)

    def test_stream_agent_end_is_result(self) -> None:
        line = json.dumps(
            {
                "type": "agent_end",
                "id": "sess-1",
                "messages": [
                    {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                ],
            }
        )
        events = parse_omp_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)
        assert events[0].result == "done"

    def test_stream_compact(self) -> None:
        line = json.dumps({"type": "compact", "trigger": "auto"})
        events = parse_omp_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], CompactBoundaryEvent)

    def test_stream_ignore_unknown(self) -> None:
        line = json.dumps({"type": "unknown_thing", "foo": "bar"})
        assert parse_omp_stream_line(line) == []


class TestOmpProvider:
    def test_find_cli_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.omp_provider.which", lambda _: None)
        with pytest.raises(FileNotFoundError, match="omp CLI not found"):
            OmpCLI(CLIConfig(provider="omp"))

    def test_build_command_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.omp_provider.which", lambda _: "/usr/bin/omp")
        cli = OmpCLI(
            CLIConfig(
                provider="omp",
                model="anthropic/claude-opus-5",
                permission_mode="bypassPermissions",
                reasoning_effort="high",
                system_prompt="SYS",
                append_system_prompt="RULES",
            )
        )
        cmd = cli._build_command("hello world")
        assert cmd[0] == "/usr/bin/omp"
        assert "--mode" in cmd
        assert cmd[cmd.index("--mode") + 1] == "json"
        assert cmd[cmd.index("--model") + 1] == "anthropic/claude-opus-5"
        assert cmd[cmd.index("--thinking") + 1] == "high"
        assert "--auto-approve" in cmd
        assert "hello world" in cmd

    def test_build_command_no_auto_approve_when_restricted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("ductor_bot.cli.omp_provider.which", lambda _: "/usr/bin/omp")
        cli = OmpCLI(CLIConfig(provider="omp", permission_mode="default"))
        cmd = cli._build_command("hi")
        assert "--auto-approve" not in cmd

    def test_build_command_resume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.omp_provider.which", lambda _: "/usr/bin/omp")
        cli = OmpCLI(CLIConfig(provider="omp"))
        cmd = cli._build_command("followup", resume_session="sess-999")
        assert cmd[cmd.index("--resume") + 1] == "sess-999"

    def test_parse_response_ok(self) -> None:
        raw = (
            json.dumps({"type": "session", "id": "s1"})
            + "\n"
            + json.dumps(
                {
                    "type": "agent_end",
                    "id": "s1",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                    ],
                }
            )
        ).encode()
        resp = _parse_response(raw, b"", 0)
        assert not resp.is_error
        assert resp.result == "ok"
        assert resp.session_id == "s1"


def _omp_task_cfg(**kwargs: Any) -> TaskExecutionConfig:
    base: dict[str, Any] = {
        "provider": "omp",
        "model": "anthropic/claude-opus-5",
        "reasoning_effort": "",
        "cli_parameters": [],
        "permission_mode": "bypassPermissions",
        "working_dir": "/tmp",
        "file_access": "all",
    }
    base.update(kwargs)
    return TaskExecutionConfig(**base)


class TestOmpCronCmd:
    def test_build_omp_cmd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "/usr/bin/omp")
        cfg = _omp_task_cfg(model="openai/gpt-5")
        cmd = _build_omp_cmd(cfg, "do the thing")
        assert cmd is not None
        assert cmd.cmd[0] == "/usr/bin/omp"
        assert "--mode" in cmd.cmd
        assert "do the thing" in cmd.cmd

    def test_build_omp_missing_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: None)
        cfg = _omp_task_cfg()
        assert _build_omp_cmd(cfg, "hi") is None


class TestFactoryAndRegistry:
    def test_factory_returns_omp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.omp_provider.which", lambda _: "/usr/bin/omp")
        cli = create_cli(CLIConfig(provider="omp"))
        assert isinstance(cli, OmpCLI)

    def test_registry_routes_omp_selector(self) -> None:
        assert ModelRegistry.provider_for("anthropic/claude-opus-5") == "omp"
        assert ModelRegistry.provider_for("openai/gpt-5") == "omp"

    def test_registry_routes_grok_still(self) -> None:
        assert ModelRegistry.provider_for("grok-4.5") == "grok"
