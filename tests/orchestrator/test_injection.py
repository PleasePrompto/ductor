"""Tests for _inject_prompt provider/model resolution from the active session."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.cli.types import AgentRequest, AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.orchestrator.injection import _inject_prompt
from ductor_bot.session import SessionKey, SessionManager
from ductor_bot.workspace.paths import DuctorPaths


def _make_orch(active: Any) -> MagicMock:
    orch = MagicMock()
    orch._task_hub = None
    orch._sessions.get_active = AsyncMock(return_value=active)
    orch.resolve_runtime_target.return_value = ("opus", "claude")
    orch._cli_service.execute = AsyncMock(
        return_value=AgentResponse(result="ok", session_id="any", is_error=False)
    )
    orch._config.cli_timeout = 60
    return orch


def _captured_request(orch: MagicMock) -> AgentRequest:
    return cast("AgentRequest", orch._cli_service.execute.await_args.args[0])


class TestInjectPromptProviderOverride:
    """`_inject_prompt` passes the active session's provider/model into AgentRequest."""

    async def test_uses_active_session_provider_model(self) -> None:
        active = MagicMock(session_id="sid", provider="codex", model="gpt-5.5")
        orch = _make_orch(active)

        with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()):
            await _inject_prompt(orch, "hi", chat_id=1, process_label="task_result:x")

        req = _captured_request(orch)
        assert req.provider_override == "codex"
        assert req.model_override == "gpt-5.5"
        assert req.resume_session == "sid"

    async def test_creates_and_persists_session_when_no_active_session(self) -> None:
        orch = _make_orch(None)
        created = MagicMock(
            session_id="",
            provider="claude",
            model="opus",
            reasoning_effort="high",
        )
        orch._sessions.resolve_session = AsyncMock(return_value=(created, True))

        with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()) as update:
            await _inject_prompt(orch, "hi", chat_id=1, process_label="task_result:x")

        req = _captured_request(orch)
        assert req.provider_override == "claude"
        assert req.model_override == "opus"
        assert req.resume_session is None
        orch._sessions.resolve_session.assert_awaited_once()
        update.assert_awaited_once_with(orch, created, orch._cli_service.execute.return_value)

    async def test_canonicalizes_transport_alias_for_session_key(self) -> None:
        orch = _make_orch(None)
        created = MagicMock(session_id="", provider="claude", model="opus")
        orch._sessions.resolve_session = AsyncMock(return_value=(created, True))

        with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()):
            await _inject_prompt(
                orch,
                "hi",
                chat_id=1,
                process_label="task_question:x",
                transport="telegram",
            )

        req = _captured_request(orch)
        assert req.transport == "tg"
        assert orch._sessions.resolve_session.await_args.args[0].transport == "tg"

    async def test_new_question_session_is_resumable_after_injection(self, tmp_path: Path) -> None:
        """A question delivered before startup's first user turn survives to the answer."""
        orch = _make_orch(None)
        orch._config = AgentConfig(provider="claude", model="opus")
        orch.paths = DuctorPaths(ductor_home=tmp_path)
        orch._sessions = SessionManager(orch.paths.sessions_path, orch._config)
        orch.resolve_runtime_target.return_value = ("opus", "claude")

        await _inject_prompt(
            orch, "[TASK QUESTION] approve?", chat_id=7, process_label="task_question:x"
        )

        persisted = await orch._sessions.get_active(SessionKey.telegram(7))
        assert persisted is not None
        assert persisted.session_id == "any"
        assert persisted.message_count == 1

    async def test_task_question_binds_parent_session_identity(self) -> None:
        active = MagicMock(
            session_id="parent-session",
            provider="claude",
            model="opus",
            reasoning_effort="high",
        )
        orch = _make_orch(active)
        orch._task_hub = MagicMock()

        with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()):
            await _inject_prompt(
                orch,
                "Which option should I use?",
                chat_id=1,
                process_label="task_question:envelope",
            )

        orch._task_hub.bind_pending_question_session.assert_called_once_with(
            1,
            None,
            session_id="parent-session",
            provider="claude",
            model="opus",
            reasoning_effort="high",
        )

    async def test_claude_active_session(self) -> None:
        active = MagicMock(session_id="sid", provider="claude", model="opus")
        orch = _make_orch(active)

        with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()):
            await _inject_prompt(orch, "hi", chat_id=1, process_label="task_result:x")

        req = _captured_request(orch)
        assert req.provider_override == "claude"
        assert req.model_override == "opus"


async def test_inject_prompt_appends_configured_files(tmp_path: Path) -> None:
    """_inject_prompt injects append_system_prompt_files from the agent workspace."""
    paths = DuctorPaths(ductor_home=tmp_path)
    paths.workspace.mkdir(parents=True, exist_ok=True)
    (paths.workspace / "PERSONA.md").write_text("You are helpful.")

    active = MagicMock(session_id="sid", provider="claude", model="opus")
    orch = _make_orch(active)
    orch.paths = paths
    orch._config.append_system_prompt_files = ["PERSONA.md"]

    with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()):
        await _inject_prompt(orch, "hi", chat_id=1, process_label="task_result:x")

    req = _captured_request(orch)
    assert req.append_system_prompt is not None
    assert "You are helpful." in req.append_system_prompt


async def test_inject_prompt_no_files_leaves_append_none(tmp_path: Path) -> None:
    """Empty append_system_prompt_files -> append_system_prompt stays None."""
    paths = DuctorPaths(ductor_home=tmp_path)
    paths.workspace.mkdir(parents=True, exist_ok=True)

    orch = _make_orch(MagicMock(session_id="sid", provider="claude", model="opus"))
    orch.paths = paths
    orch._config.append_system_prompt_files = []

    with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()):
        await _inject_prompt(orch, "hi", chat_id=1, process_label="task_result:x")

    req = _captured_request(orch)
    assert req.append_system_prompt is None
