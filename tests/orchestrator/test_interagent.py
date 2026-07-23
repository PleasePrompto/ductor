"""Tests for orchestrator inter-agent Named Session handling."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import AgentConfig
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.injection import (
    _get_or_create_interagent_session,
    _interagent_chat_id,
)
from ductor_bot.session.named import interagent_session_name
from ductor_bot.workspace.paths import DuctorPaths

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def orch_ia(workspace: tuple[DuctorPaths, AgentConfig]) -> Orchestrator:
    """Orchestrator with mocked CLIService for inter-agent tests."""
    paths, config = workspace
    config.allowed_user_ids = [12345]
    o = Orchestrator(config, paths, agent_name="codex")
    mock_cli = MagicMock()
    mock_cli._config = MagicMock()
    mock_cli._config.agent_name = "codex"
    mock_cli._config.cli_timeout = 120
    mock_cli.execute = AsyncMock(return_value=CLIResponse(session_id="sess-001", result="done"))
    object.__setattr__(o, "_cli_service", mock_cli)
    return o


class TestInteragentChatId:
    """Test _interagent_chat_id helper."""

    def test_returns_first_allowed_user(self, orch_ia: Orchestrator) -> None:
        assert _interagent_chat_id(orch_ia) == 12345

    def test_returns_zero_when_no_users(self, workspace: tuple[DuctorPaths, AgentConfig]) -> None:
        paths, config = workspace
        config.allowed_user_ids = []
        o = Orchestrator(config, paths)
        assert _interagent_chat_id(o) == 0


class TestGetOrCreateInteragentSession:
    """Test _get_or_create_interagent_session."""

    def test_creates_new_session(self, orch_ia: Orchestrator) -> None:
        ns, is_new, notice = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new is True
        assert notice == ""
        assert ns.name == "ia-main"
        assert ns.chat_id == 12345
        assert ns.status == "running"

    def test_reuses_existing_session(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "main")
        ns1.status = "idle"
        ns2, is_new2, notice = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new2 is False
        assert notice == ""
        assert ns2.name == ns1.name

    def test_new_session_flag_resets_existing(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "main")
        ns1.status = "idle"
        ns1.session_id = "old-session"

        ns2, is_new, _ = _get_or_create_interagent_session(orch_ia, "main", new_session=True)
        assert is_new is True
        assert ns2.session_id == ""  # Fresh session, no resume ID

    def test_different_senders_get_different_sessions(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "alice")
        ns2, _, _ = _get_or_create_interagent_session(orch_ia, "bob")
        assert ns1.name == "ia-alice"
        assert ns2.name == "ia-bob"
        assert ns1.name != ns2.name

    def test_ended_session_creates_new_one(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "main")
        ns1.status = "ended"

        ns2, is_new, _ = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new is True
        assert ns2.session_id == ""

    def test_provider_switch_resets_session(self, orch_ia: Orchestrator) -> None:
        # Start with a codex model so the session is created for provider "codex"
        orch_ia._config.model = "gpt-5.3-codex"
        ns1, _, notice1 = _get_or_create_interagent_session(orch_ia, "main")
        assert notice1 == ""
        assert ns1.provider == "codex"
        ns1.status = "idle"
        ns1.session_id = "codex-sess-1"

        # Switch to a claude model → different provider
        orch_ia._config.model = "sonnet"

        ns2, is_new, notice2 = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new is True
        assert ns2.session_id == ""  # Fresh — old codex session discarded
        assert "provider" in notice2.lower()
        assert ns2.provider == "claude"

    def test_same_provider_no_notice(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "main")
        ns1.status = "idle"

        _ns2, is_new, notice = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new is False
        assert notice == ""

    def test_scopes_names_by_source_chat_and_topic(self) -> None:
        topic_10 = interagent_session_name("main", 777, 10)
        topic_20 = interagent_session_name("main", 777, 20)
        assert topic_10 != topic_20
        assert topic_10.startswith("ia.main.t10.x")
        assert len(topic_10) <= 40
        assert interagent_session_name("main") == "ia-main"


class TestHandleInteragentMessage:
    """Test handle_interagent_message."""

    async def test_returns_result_and_session_name(self, orch_ia: Orchestrator) -> None:
        result_text, session_name, notice = await orch_ia.handle_interagent_message(
            "main", "Do something"
        )
        assert result_text == "done"
        assert session_name == "ia-main"
        assert notice == ""

    async def test_creates_named_session(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Task 1")
        ns = orch_ia._named_sessions.get(12345, "ia-main")
        assert ns is not None
        assert ns.session_id == "sess-001"
        assert ns.status == "idle"

    async def test_resumes_existing_session(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Task 1")

        # Second call should resume with the session_id
        orch_ia._cli_service.execute = AsyncMock(
            return_value=CLIResponse(session_id="sess-002", result="continued")
        )
        result_text, _, _ = await orch_ia.handle_interagent_message("main", "Task 2")
        assert result_text == "continued"

        # Verify resume_session was passed
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert request.resume_session == "sess-001"

    async def test_new_session_flag_starts_fresh(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Task 1")

        orch_ia._cli_service.execute = AsyncMock(
            return_value=CLIResponse(session_id="sess-new", result="fresh start")
        )
        result_text, _, _ = await orch_ia.handle_interagent_message(
            "main", "New task", new_session=True
        )
        assert result_text == "fresh start"

        # Verify resume_session is None (fresh session)
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert request.resume_session is None

    async def test_same_topic_reuses_scoped_session(self, orch_ia: Orchestrator) -> None:
        _, first_name, _ = await orch_ia.handle_interagent_message(
            "main", "Task 1", source_chat_id=777, source_topic_id=10
        )
        _, second_name, _ = await orch_ia.handle_interagent_message(
            "main", "Task 2", source_chat_id=777, source_topic_id=10
        )
        assert second_name == first_name
        assert orch_ia._cli_service.execute.call_args.args[0].resume_session == "sess-001"

    async def test_different_topics_use_different_scoped_sessions(
        self, orch_ia: Orchestrator
    ) -> None:
        _, topic_10, _ = await orch_ia.handle_interagent_message(
            "main", "Task 1", source_chat_id=777, source_topic_id=10
        )
        _, topic_20, _ = await orch_ia.handle_interagent_message(
            "main", "Task 2", source_chat_id=777, source_topic_id=20
        )
        assert topic_10 != topic_20

    async def test_new_keeps_scoped_destination(self, orch_ia: Orchestrator) -> None:
        _, first_name, _ = await orch_ia.handle_interagent_message(
            "main", "Task 1", source_chat_id=777, source_topic_id=10
        )
        _, new_name, _ = await orch_ia.handle_interagent_message(
            "main",
            "Task 2",
            new_session=True,
            source_chat_id=777,
            source_topic_id=10,
        )
        assert new_name == first_name
        assert orch_ia._cli_service.execute.call_args.args[0].resume_session is None

    async def test_interagent_request_uses_recipient_anchor(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message(
            "main", "Task", source_chat_id=777, source_topic_id=10
        )
        request = orch_ia._cli_service.execute.call_args.args[0]
        assert request.chat_id == 12345
        assert request.chat_id != 777

    async def test_task_delivery_keeps_recipient_anchor(
        self, orch_ia: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ductor_bot.cli.base import CLIConfig
        from ductor_bot.cli.executor import build_subprocess_env

        await orch_ia.handle_interagent_message(
            "main", "Create a task", source_chat_id=777, source_topic_id=10
        )
        request = orch_ia._cli_service.execute.call_args.args[0]
        assert request.chat_id == 12345

        monkeypatch.delenv("DUCTOR_CHAT_ID", raising=False)
        monkeypatch.delenv("DUCTOR_TOPIC_ID", raising=False)
        env = build_subprocess_env(
            CLIConfig(
                working_dir="/tmp/workspace",
                chat_id=request.chat_id,
                topic_id=request.topic_id,
                transport=request.transport,
            )
        )
        assert env is not None
        monkeypatch.setenv("DUCTOR_CHAT_ID", env["DUCTOR_CHAT_ID"])

        tool = _ROOT / "ductor_bot/_home_defaults/workspace/tools/task_tools/create_task.py"
        spec = importlib.util.spec_from_file_location("create_task_tool", tool)
        assert spec is not None
        assert spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        captured: dict[str, object] = {}

        def post_json(_url: str, body: dict[str, object], *, timeout: int) -> dict[str, object]:
            assert timeout == 10
            captured.update(body)
            return {"success": True, "task_id": "task-1"}

        monkeypatch.setattr(
            mod,
            "_load_shared",
            lambda: (lambda _path: "http://example.invalid", post_json, lambda: "codex"),
        )
        monkeypatch.setattr(sys, "argv", ["create_task.py", "do work"])
        mod.main()
        assert captured["chat_id"] == 12345
        assert "topic_id" not in captured

    async def test_prompt_contains_interagent_markers(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Hello world")
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert "[INTER-AGENT MESSAGE from 'main'" in request.prompt
        assert "Hello world" in request.prompt
        assert "[END INTER-AGENT MESSAGE]" in request.prompt

    async def test_process_label_set_correctly(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Test")
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert request.process_label == "interagent:main"

    async def test_error_returns_error_text(self, orch_ia: Orchestrator) -> None:
        orch_ia._cli_service.execute = AsyncMock(side_effect=RuntimeError("crash"))
        result_text, session_name, _ = await orch_ia.handle_interagent_message("main", "Crash")
        assert "Error" in result_text
        assert session_name == "ia-main"

    async def test_provider_switch_returns_notice(self, orch_ia: Orchestrator) -> None:
        # Start with codex provider
        orch_ia._config.model = "gpt-5.3-codex"
        await orch_ia.handle_interagent_message("main", "Task 1")

        # Switch to claude provider
        orch_ia._config.model = "sonnet"
        orch_ia._cli_service.execute = AsyncMock(
            return_value=CLIResponse(session_id="claude-sess", result="switched")
        )
        result_text, _, notice = await orch_ia.handle_interagent_message("main", "Task 2")
        assert result_text == "switched"
        assert "provider" in notice.lower()
        # Fresh session → no resume
        call_args = orch_ia._cli_service.execute.call_args
        assert call_args[0][0].resume_session is None

    async def test_session_idle_after_error(self, orch_ia: Orchestrator) -> None:
        orch_ia._cli_service.execute = AsyncMock(side_effect=RuntimeError("crash"))
        await orch_ia.handle_interagent_message("main", "Crash")
        ns = orch_ia._named_sessions.get(12345, "ia-main")
        assert ns is not None
        assert ns.status == "idle"

    async def test_handle_interagent_message_recovers_from_stale_session(
        self, orch_ia: Orchestrator
    ) -> None:
        """#81: when the first cli.execute returns a stale-session error, the
        handler must end the named session, create a fresh one, retry without
        ``resume_session``, and prepend a recovery notice to
        ``provider_switch_notice`` so the caller sees what happened."""
        # Seed an idle session with a (about to be) stale session_id.
        await orch_ia.handle_interagent_message("main", "Task 1")
        ns = orch_ia._named_sessions.get(12345, "ia-main")
        assert ns is not None
        ns.session_id = "stale-id"
        ns.status = "idle"

        stale_response = CLIResponse(
            result="No conversation found with session ID: stale-id",
            is_error=True,
        )
        fresh_response = CLIResponse(session_id="fresh-sess", result="fresh response")
        orch_ia._cli_service.execute = AsyncMock(side_effect=[stale_response, fresh_response])

        result_text, session_name, notice = await orch_ia.handle_interagent_message(
            "main", "Task 2"
        )

        assert result_text == "fresh response"
        assert session_name == "ia-main"
        assert "stale" in notice.lower()
        assert orch_ia._cli_service.execute.call_count == 2
        first_call_req = orch_ia._cli_service.execute.call_args_list[0][0][0]
        second_call_req = orch_ia._cli_service.execute.call_args_list[1][0][0]
        assert first_call_req.resume_session == "stale-id"
        assert second_call_req.resume_session is None


def test_ask_agent_forwards_source_chat_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _ROOT / "ductor_bot/_home_defaults/workspace/tools/agent_tools/ask_agent.py"
    spec = importlib.util.spec_from_file_location("ask_agent_tool", tool)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"success": True, "text": "ok"}
    ).encode()
    captured: dict[str, object] = {}

    def urlopen(request: object, timeout: int) -> MagicMock:
        assert timeout == 300
        captured.update(json.loads(request.data.decode()))
        return response

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "argv", ["ask_agent.py", "codex", "hello"])
    monkeypatch.setenv("DUCTOR_AGENT_NAME", "main")
    monkeypatch.setenv("DUCTOR_CHAT_ID", "777")
    monkeypatch.setenv("DUCTOR_TOPIC_ID", "10")
    mod.main()
    assert captured["chat_id"] == 777
    assert captured["topic_id"] == 10
