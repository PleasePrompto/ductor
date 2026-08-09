"""Focused tests for the readable active named-session Telegram flow."""

from __future__ import annotations

from unittest.mock import AsyncMock

from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.session.key import SessionKey


async def test_active_named_target_routes_plain_text(orch, monkeypatch) -> None:
    key = SessionKey.telegram(1)
    ns = orch._named_sessions.create(1, "codex", "gpt-5.4", "Review the release checklist", key=key)
    orch._named_sessions.update_after_response(1, ns.name, "sid")
    orch.switch_named_target(key, ns.name)
    route = AsyncMock(return_value=OrchestratorResult(text="named reply"))
    monkeypatch.setattr("ductor_bot.orchestrator.core.named_session_flow", route)
    monkeypatch.setattr(orch, "_ensure_docker", AsyncMock())

    result = await orch.handle_message(key, "continue with the rollback plan")

    assert result.text == "named reply"
    route.assert_awaited_once_with(orch, key, ns.name, "continue with the rollback plan")


async def test_free_text_sessions_opens_selector(orch, monkeypatch) -> None:
    monkeypatch.setattr(orch, "_ensure_docker", AsyncMock())
    monkeypatch.setattr(
        "ductor_bot.orchestrator.core.cmd_sessions",
        AsyncMock(return_value=OrchestratorResult(text="selector")),
    )

    result = await orch.handle_message(SessionKey.telegram(1), "sessions")

    assert result.text == "selector"


async def test_pending_rename_consumes_next_message_without_running_a_turn(orch, monkeypatch) -> None:
    key = SessionKey.telegram(1)
    ns = orch._named_sessions.create(1, "codex", "gpt-5.4", "Original", key=key)
    assert orch._named_sessions.begin_rename(key, ns.name)
    monkeypatch.setattr(orch, "_ensure_docker", AsyncMock())

    result = await orch.handle_message(key, "Release checklist")

    assert result.text == "Renamed session to: Release checklist"
    assert orch.get_named_session(1, ns.name).display_title == "Release checklist"  # type: ignore[union-attr]
