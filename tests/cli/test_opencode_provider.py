"""Unit tests for opencode provider event parsing and command building."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.factory import create_cli
from ductor_bot.cli.opencode_aliases import (
    expand_opencode_model_alias,
    shorten_opencode_model_id,
)
from ductor_bot.cli.opencode_events import (
    extract_opencode_session_id,
    parse_opencode_json,
    parse_opencode_stream_line,
)
from ductor_bot.cli.opencode_provider import OpencodeCLI, _parse_response
from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ductor_bot.config import ModelRegistry
from ductor_bot.cron.execution import _build_opencode_cmd


def _line(event_type: str, session_id: str, **extra: Any) -> str:
    payload = {"type": event_type, "timestamp": 1_700_000_000_000, "sessionID": session_id}
    payload.update(extra)
    return json.dumps(payload)


class TestOpencodeEvents:
    def test_parse_json_happy_path(self) -> None:
        raw = "\n".join(
            (
                _line("step_start", "ses_1", part={"type": "step-start"}),
                _line("text", "ses_1", part={"type": "text", "text": "hello"}),
                _line(
                    "step_finish",
                    "ses_1",
                    part={
                        "type": "step-finish",
                        "reason": "stop",
                        "tokens": {"input": 10, "output": 3, "total": 13, "cost": 0.0123},
                    },
                ),
                _line("text", "ses_1", part={"type": "text", "text": " world"}),
            )
        )
        text, session_id, usage, model_usage, turns, is_error, cost = parse_opencode_json(raw)
        assert text == "hello\n world"
        assert session_id == "ses_1"
        assert usage["input_tokens"] == 10
        assert usage["output_tokens"] == 3
        assert usage["total_tokens"] == 13
        assert model_usage == {}
        assert turns == 1
        assert is_error is False
        assert cost == 0.0123

    def test_parse_json_error_event(self) -> None:
        raw = _line(
            "error",
            "ses_e",
            error={"name": "Error", "data": {"message": "provider not configured"}},
        )
        text, _sid, _u, _m, _t, is_error, _c = parse_opencode_json(raw)
        assert is_error is True
        assert "provider not configured" in text

    def test_parse_json_real_step_finish_shape(self) -> None:
        """Real opencode step_finish: cost sits at the part level (sibling of tokens)."""
        raw = "\n".join(
            (
                _line("step_start", "ses_1", part={"type": "step-start"}),
                _line("text", "ses_1", part={"type": "text", "text": "parser-ok"}),
                _line(
                    "step_finish",
                    "ses_1",
                    part={
                        "type": "step-finish",
                        "reason": "stop",
                        "tokens": {
                            "total": 7865,
                            "input": 7861,
                            "output": 4,
                            "reasoning": 0,
                            "cache": {"write": 0, "read": 0},
                        },
                        "cost": 0.00173206,
                    },
                ),
            )
        )
        text, session_id, usage, _m, turns, is_error, cost = parse_opencode_json(raw)
        assert text == "parser-ok"
        assert session_id == "ses_1"
        assert usage == {"input_tokens": 7861, "output_tokens": 4, "total_tokens": 7865}
        assert turns == 1
        assert is_error is False
        assert cost == 0.00173206

    def test_parse_json_plain_text_fallback(self) -> None:
        text, session_id, _u, _m, _t, is_error, _c = parse_opencode_json("not json at all")
        assert text == "not json at all"
        assert session_id is None
        assert is_error is False

    def test_parse_json_empty(self) -> None:
        text, _sid, _u, _m, _t, is_error, _c = parse_opencode_json("   ")
        assert text == ""
        assert is_error is True

    def test_parse_stream_text(self) -> None:
        events = parse_opencode_stream_line(
            _line("text", "ses_1", part={"type": "text", "text": "hi"})
        )
        assert len(events) == 1
        assert isinstance(events[0], AssistantTextDelta)
        assert events[0].text == "hi"

    def test_parse_stream_reasoning(self) -> None:
        events = parse_opencode_stream_line(
            _line("reasoning", "ses_1", part={"type": "reasoning", "text": "thinking..."})
        )
        assert len(events) == 1
        assert isinstance(events[0], ThinkingEvent)
        assert events[0].text == "thinking..."

    def test_parse_stream_tool_use(self) -> None:
        events = parse_opencode_stream_line(
            _line(
                "tool_use",
                "ses_1",
                part={
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "completed", "input": {"command": "ls"}},
                },
            )
        )
        assert len(events) == 1
        assert isinstance(events[0], ToolUseEvent)
        assert events[0].tool_name == "bash"
        assert events[0].parameters == {"command": "ls"}

    def test_parse_stream_error(self) -> None:
        events = parse_opencode_stream_line(
            _line("error", "ses_e", error={"name": "Error", "data": {"message": "boom"}})
        )
        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)
        assert events[0].is_error is True
        assert events[0].result == "boom"
        assert events[0].session_id == "ses_e"

    def test_parse_stream_ignores_unknown_and_step_events(self) -> None:
        assert parse_opencode_stream_line(_line("step_start", "ses_1")) == []
        assert parse_opencode_stream_line(_line("step_finish", "ses_1")) == []
        assert parse_opencode_stream_line('{"type":"totally_unknown"}') == []
        assert parse_opencode_stream_line("not json") == []

    def test_extract_session_id(self) -> None:
        assert extract_opencode_session_id(_line("text", "ses_9")) == "ses_9"
        assert extract_opencode_session_id("not json") is None
        assert extract_opencode_session_id('{"type":"text"}') is None

    def test_expand_provider_aliases(self) -> None:
        assert expand_opencode_model_alias("go/model-alpha") == ("opencode-go/model-alpha")
        assert expand_opencode_model_alias("zen/model-beta") == ("opencode/model-beta")
        assert expand_opencode_model_alias("opencode-zen/model-beta") == ("opencode/model-beta")
        assert expand_opencode_model_alias("or/provider/model-gamma") == (
            "openrouter/provider/model-gamma"
        )
        assert expand_opencode_model_alias("unknown/model") == "unknown/model"
        assert shorten_opencode_model_id("opencode-go/model-alpha") == ("@go/model-alpha")
        assert shorten_opencode_model_id("opencode/model-beta") == ("@zen/model-beta")
        assert shorten_opencode_model_id("openrouter/provider/model-gamma") == (
            "@or/provider/model-gamma"
        )


class TestOpencodeProvider:
    def test_find_cli_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.opencode_provider.which", lambda _: None)
        with pytest.raises(FileNotFoundError, match="opencode CLI not found"):
            OpencodeCLI(CLIConfig(provider="opencode"))

    def test_build_command_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.opencode_provider.which", lambda _: "/usr/bin/opencode")
        cli = OpencodeCLI(
            CLIConfig(
                provider="opencode",
                model="opencode/model-one",
                permission_mode="bypassPermissions",
            )
        )
        cmd = cli._build_command("hello world")
        assert cmd[0] == "/usr/bin/opencode"
        assert cmd[1] == "run"
        assert "--format" in cmd
        assert "json" in cmd
        assert "--auto" in cmd
        assert cmd[cmd.index("--model") + 1] == "opencode/model-one"
        assert cmd[-1] == "hello world"

    def test_build_command_no_auto_when_not_bypass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.opencode_provider.which", lambda _: "/usr/bin/opencode")
        cli = OpencodeCLI(CLIConfig(provider="opencode", permission_mode="default"))
        cmd = cli._build_command("hi")
        assert "--auto" not in cmd

    def test_build_command_resume_and_continue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.opencode_provider.which", lambda _: "/usr/bin/opencode")
        cli = OpencodeCLI(CLIConfig(provider="opencode"))
        resume = cli._build_command("follow up", resume_session="ses_abc")
        assert resume[resume.index("--session") + 1] == "ses_abc"
        cont = cli._build_command("follow up", continue_session=True)
        assert "--continue" in cont
        assert "--session" not in cont

    def test_build_command_long_prompt_uses_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.opencode_provider.which", lambda _: "/usr/bin/opencode")
        monkeypatch.setattr("ductor_bot.cli.opencode_provider._IS_WINDOWS", False)
        cli = OpencodeCLI(CLIConfig(provider="opencode"))
        long_prompt = "x" * 30_000
        assert cli._use_stdin_prompt(long_prompt) is True
        cmd = cli._build_command(long_prompt, stdin_prompt=True)
        assert cmd[-1] != long_prompt
        assert long_prompt not in cmd

    def test_parse_response_ndjson(self) -> None:
        raw = "\n".join(
            (
                _line("text", "ses_1", part={"type": "text", "text": "answer"}),
                _line(
                    "step_finish",
                    "ses_1",
                    part={"type": "step-finish", "tokens": {"input": 5, "output": 2}},
                ),
            )
        ).encode()
        resp = _parse_response(raw, b"", 0)
        assert resp.result == "answer"
        assert resp.session_id == "ses_1"
        assert resp.is_error is False
        assert resp.usage["input_tokens"] == 5
        assert resp.usage["output_tokens"] == 2

    def test_parse_response_error(self) -> None:
        raw = _line(
            "error", "ses_e", error={"name": "Error", "data": {"message": "no creds"}}
        ).encode()
        resp = _parse_response(raw, b"", 1)
        assert resp.is_error is True
        assert "no creds" in resp.result

    def test_parse_response_empty(self) -> None:
        resp = _parse_response(b"", b"boom", 1)
        assert resp.is_error is True
        assert "boom" in resp.result

    def test_docker_container_uses_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OpenCode uses the configured sandbox, just like other providers."""
        monkeypatch.setattr("ductor_bot.cli.opencode_provider.which", lambda _: "/usr/bin/opencode")
        cli = OpencodeCLI(
            CLIConfig(
                provider="opencode",
                docker_container="ductor-sandbox",
                working_dir="/root/.ductor/workspace",
            )
        )
        cmd = cli._build_command("hi")
        exec_cmd, use_cwd = cli._resolve_exec(cmd, stdin_prompt=False)
        assert exec_cmd[:2] == ["docker", "exec"]
        assert "-w" in exec_cmd
        assert "/ductor/workspace" in exec_cmd
        assert "ductor-sandbox" in exec_cmd
        assert "run" in exec_cmd
        assert "/root/.opencode/bin/opencode" not in exec_cmd
        assert "opencode" in exec_cmd
        assert exec_cmd[-1] == "hi"
        assert use_cwd is None

    def test_docker_container_keeps_stdin_open_for_long_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("ductor_bot.cli.opencode_provider.which", lambda _: "/usr/bin/opencode")
        cli = OpencodeCLI(
            CLIConfig(
                provider="opencode",
                docker_container="ductor-sandbox",
                working_dir="/root/.ductor/workspace",
            )
        )
        cmd = cli._build_command("x" * 30_000, stdin_prompt=True)
        exec_cmd, _use_cwd = cli._resolve_exec(cmd, stdin_prompt=True)
        assert exec_cmd[:3] == ["docker", "exec", "-i"]


def _opencode_task_cfg(**kwargs: Any) -> TaskExecutionConfig:
    base: dict[str, Any] = {
        "provider": "opencode",
        "model": "opencode/model-one",
        "reasoning_effort": "medium",
        "permission_mode": "bypassPermissions",
        "cli_parameters": [],
        "working_dir": "/tmp",
        "file_access": "all",
    }
    base.update(kwargs)
    return TaskExecutionConfig(**base)


class TestOpencodeCronCmd:
    def test_short_prompt_positional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "/usr/bin/opencode")
        one = _build_opencode_cmd(_opencode_task_cfg(), "hello")
        assert one is not None
        assert one.stdin_input is None
        assert one.cmd[1] == "run"
        assert "--auto" in one.cmd
        assert one.cmd[-1] == "hello"

    def test_long_prompt_uses_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "/usr/bin/opencode")
        long_prompt = "y" * 30_000
        one = _build_opencode_cmd(_opencode_task_cfg(), long_prompt)
        assert one is not None
        assert one.stdin_input == long_prompt.encode()
        assert long_prompt not in one.cmd


class TestFactoryAndRegistry:
    def test_factory_returns_opencode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.opencode_provider.which", lambda _: "/usr/bin/opencode")
        cli = create_cli(CLIConfig(provider="opencode", model="provider-a/model-one"))
        assert isinstance(cli, OpencodeCLI)

    def test_model_registry_routes_opencode(self) -> None:
        assert ModelRegistry.provider_for("opencode/model-one") == "opencode"
        assert ModelRegistry.provider_for("provider-a/model-one") == "opencode"
        assert ModelRegistry.provider_for("provider-b/model-two") == "opencode"
        # Bare IDs (no "/") still route to their own providers / codex fallback.
        assert ModelRegistry.provider_for("opus") == "claude"
        assert ModelRegistry.provider_for("grok-4.5") == "grok"
        assert ModelRegistry.provider_for("unknown-bare-model") == "codex"
