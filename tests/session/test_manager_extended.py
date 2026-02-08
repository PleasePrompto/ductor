"""Extended session manager tests -- covering gaps from audit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


# -- max_session_messages limit --


async def test_session_expires_after_max_messages(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, max_session_messages=3)
    s1, _ = await mgr.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr, s1, "cli-id-1")

    # Send 3 messages (reaches limit)
    for _ in range(2):  # already 1 from simulate
        await mgr.update_session(s1)

    assert s1.message_count == 3

    # Next resolve should create a NEW session
    s2, is_new = await mgr.resolve_session(chat_id=1)
    assert is_new is True
    assert s2.session_id == ""


async def test_session_stays_fresh_below_max_messages(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, max_session_messages=5)
    s1, _ = await mgr.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr, s1, "cli-id-1")

    for _ in range(3):  # already 1 from simulate -> total 4
        await mgr.update_session(s1)

    s2, is_new = await mgr.resolve_session(chat_id=1)
    assert is_new is False
    assert s2.session_id == "cli-id-1"


async def test_max_messages_none_means_unlimited(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, max_session_messages=None)
    s1, _ = await mgr.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr, s1, "cli-id-1")

    for _ in range(100):
        await mgr.update_session(s1)

    _s2, is_new = await mgr.resolve_session(chat_id=1)
    assert is_new is False


# -- corrupt session file recovery --


async def test_corrupt_session_file_recovers(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text("THIS IS NOT JSON {{{", encoding="utf-8")

    mgr = _make_manager(tmp_path)
    session, is_new = await mgr.resolve_session(chat_id=1)
    assert is_new is True
    assert session.session_id == ""  # Empty until CLI fills it


async def test_empty_session_file_recovers(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text("", encoding="utf-8")

    mgr = _make_manager(tmp_path)
    _session, is_new = await mgr.resolve_session(chat_id=1)
    assert is_new is True
