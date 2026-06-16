"""Tests for _inject_prompt provider/model resolution from the active session."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.cli.types import AgentRequest, AgentResponse
from ductor_bot.orchestrator.injection import _inject_prompt
from ductor_bot.workspace.paths import DuctorPaths


def _make_orch(active: Any) -> MagicMock:
    orch = MagicMock()
    orch._sessions.get_active = AsyncMock(return_value=active)
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

    async def test_falls_back_to_none_when_no_active_session(self) -> None:
        orch = _make_orch(None)

        with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()):
            await _inject_prompt(orch, "hi", chat_id=1, process_label="task_result:x")

        req = _captured_request(orch)
        assert req.provider_override is None
        assert req.model_override is None
        assert req.resume_session is None

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
