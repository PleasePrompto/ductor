"""Tests for JSON-based session manager."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import time_machine

from ductor_bot.config import AgentConfig
from ductor_bot.session.manager import SessionData, SessionManager


def _make_manager(tmp_path: Path, **overrides: Any) -> SessionManager:
    cfg = AgentConfig(**overrides)
    return SessionManager(sessions_path=tmp_path / "sessions.json", config=cfg)


async def _simulate_cli_response(
    mgr: SessionManager, session: SessionData, cli_session_id: str
) -> None:
    """Simulate the orchestrator storing the CLI-assigned session ID."""
    session.session_id = cli_session_id
    await mgr.update_session(session)


async def test_resolve_creates_new_session(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    session, is_new = await mgr.resolve_session(chat_id=1)
    assert is_new is True
    assert session.chat_id == 1
    assert session.session_id == ""


async def test_resolve_reuses_fresh_session(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, idle_timeout_minutes=30)
    s1, new1 = await mgr.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr, s1, "cli-assigned-id")

    s2, new2 = await mgr.resolve_session(chat_id=1)
    assert new1 is True
    assert new2 is False
    assert s2.session_id == "cli-assigned-id"


async def test_resolve_treats_empty_session_id_as_new(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, idle_timeout_minutes=30)
    _s1, _ = await mgr.resolve_session(chat_id=1)
    # Don't simulate CLI response -- session_id stays empty
    s2, new2 = await mgr.resolve_session(chat_id=1)
    assert new2 is True
    assert s2.session_id == ""


@time_machine.travel("2025-06-15 12:00:00", tick=False)
async def test_session_expires_after_idle_timeout(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, idle_timeout_minutes=30)
    s1, _ = await mgr.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr, s1, "cli-id-1")

    with time_machine.travel("2025-06-15 12:29:00", tick=False):
        s2, new2 = await mgr.resolve_session(chat_id=1)
        assert new2 is False
        assert s2.session_id == "cli-id-1"

    with time_machine.travel("2025-06-15 12:31:00", tick=False):
        s3, new3 = await mgr.resolve_session(chat_id=1)
        assert new3 is True
        assert s3.session_id == ""


@time_machine.travel("2025-06-15 03:30:00+00:00", tick=False)
async def test_session_expires_at_daily_reset(tmp_path: Path) -> None:
    mgr = _make_manager(
        tmp_path, idle_timeout_minutes=120, daily_reset_hour=4, user_timezone="UTC"
    )
    s1, _ = await mgr.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr, s1, "cli-id-1")

    with time_machine.travel("2025-06-15 04:01:00+00:00", tick=False):
        s2, new2 = await mgr.resolve_session(chat_id=1)
        assert new2 is True
        assert s2.session_id == ""


async def test_provider_switch_resets_session(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s1, _ = await mgr.resolve_session(chat_id=1, provider="claude")
    await _simulate_cli_response(mgr, s1, "claude-session-id")

    s2, new2 = await mgr.resolve_session(chat_id=1, provider="codex")
    assert new2 is True
    assert s2.session_id == ""
    assert s2.provider == "codex"


async def test_reset_session(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s1, _ = await mgr.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr, s1, "cli-id-1")

    s2 = await mgr.reset_session(chat_id=1)
    assert s2.session_id == ""
    assert s2.message_count == 0


async def test_update_session_increments(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s, _ = await mgr.resolve_session(chat_id=1)
    assert s.message_count == 0
    await mgr.update_session(s, cost_usd=0.05, tokens=1000)
    assert s.message_count == 1
    assert s.total_cost_usd == 0.05
    assert s.total_tokens == 1000


async def test_update_session_accumulates(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s, _ = await mgr.resolve_session(chat_id=1)
    await mgr.update_session(s, cost_usd=0.01, tokens=100)
    await mgr.update_session(s, cost_usd=0.02, tokens=200)
    assert s.message_count == 2
    assert s.total_cost_usd == pytest.approx(0.03)
    assert s.total_tokens == 300


async def test_persistence_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    cfg = AgentConfig()

    mgr1 = SessionManager(sessions_path=path, config=cfg)
    s1, _ = await mgr1.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr1, s1, "persisted-id")
    await mgr1.update_session(s1, cost_usd=0.1, tokens=500)

    mgr2 = SessionManager(sessions_path=path, config=cfg)
    s2, new2 = await mgr2.resolve_session(chat_id=1)
    assert new2 is False
    assert s2.session_id == "persisted-id"
    assert s2.total_cost_usd == pytest.approx(0.1)


async def test_session_data_defaults() -> None:
    s = SessionData(session_id="abc", chat_id=1)
    assert s.provider == "claude"
    assert s.message_count == 0
    assert s.total_cost_usd == 0.0
    assert s.total_tokens == 0


async def test_separate_chat_ids(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s1, n1 = await mgr.resolve_session(chat_id=1)
    s2, n2 = await mgr.resolve_session(chat_id=2)
    assert n1 is True
    assert n2 is True
    assert s1.chat_id != s2.chat_id
