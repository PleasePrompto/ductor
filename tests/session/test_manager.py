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
        tmp_path,
        idle_timeout_minutes=120,
        daily_reset_hour=4,
        daily_reset_enabled=True,
        user_timezone="UTC",
    )
    s1, _ = await mgr.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr, s1, "cli-id-1")

    with time_machine.travel("2025-06-15 04:01:00+00:00", tick=False):
        s2, new2 = await mgr.resolve_session(chat_id=1)
        assert new2 is True
        assert s2.session_id == ""


@time_machine.travel("2025-06-15 03:30:00+00:00", tick=False)
async def test_daily_reset_disabled_by_default(tmp_path: Path) -> None:
    """When daily_reset_enabled=False (default), the reset_hour has no effect."""
    mgr = _make_manager(tmp_path, idle_timeout_minutes=0, daily_reset_hour=4, user_timezone="UTC")
    s1, _ = await mgr.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr, s1, "cli-id-1")

    with time_machine.travel("2025-06-15 04:01:00+00:00", tick=False):
        s2, new2 = await mgr.resolve_session(chat_id=1)
        # Reset should NOT happen because daily_reset_enabled defaults to False
        assert new2 is False
        assert s2.session_id == "cli-id-1"


@time_machine.travel("2025-06-15 01:00:00+00:00", tick=False)
async def test_daily_reset_over_midnight(tmp_path: Path) -> None:
    """Session created before yesterday's reset_hour must expire even when now is before today's."""
    # Session created yesterday at 02:00, before the 04:00 reset
    with time_machine.travel("2025-06-14 02:00:00+00:00", tick=False):
        mgr = _make_manager(
            tmp_path,
            idle_timeout_minutes=0,
            daily_reset_hour=4,
            daily_reset_enabled=True,
            user_timezone="UTC",
        )
        s1, _ = await mgr.resolve_session(chat_id=1)
        await _simulate_cli_response(mgr, s1, "old-session")

    # Now it's 01:00 today — before today's 04:00 reset, but after yesterday's 04:00 reset
    s2, new2 = await mgr.resolve_session(chat_id=1)
    assert new2 is True
    assert s2.session_id == ""


@time_machine.travel("2025-06-15 01:00:00+00:00", tick=False)
async def test_daily_reset_not_triggered_for_recent_session(tmp_path: Path) -> None:
    """Session created after yesterday's reset_hour should survive until today's reset."""
    # Session created yesterday at 06:00 — after the 04:00 reset
    with time_machine.travel("2025-06-14 06:00:00+00:00", tick=False):
        mgr = _make_manager(
            tmp_path,
            idle_timeout_minutes=0,
            daily_reset_hour=4,
            daily_reset_enabled=True,
            user_timezone="UTC",
        )
        s1, _ = await mgr.resolve_session(chat_id=1)
        await _simulate_cli_response(mgr, s1, "recent-session")

    # Now it's 01:00 today — before today's 04:00 reset, session should still be fresh
    s2, new2 = await mgr.resolve_session(chat_id=1)
    assert new2 is False
    assert s2.session_id == "recent-session"


async def test_update_session_serialized(tmp_path: Path) -> None:
    """Concurrent update_session calls must not lose increments (lost-update guard)."""
    import asyncio as _asyncio

    mgr = _make_manager(tmp_path)
    s, _ = await mgr.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr, s, "sess-id")

    # Fire 10 concurrent updates
    await _asyncio.gather(*[mgr.update_session(s, cost_usd=0.01, tokens=10) for _ in range(10)])

    # 1 from _simulate_cli_response + 10 from gather = 11; all increments must survive
    reloaded, _ = await mgr.resolve_session(chat_id=1)
    assert reloaded.message_count == 11
    assert reloaded.total_cost_usd == pytest.approx(0.10)
    assert reloaded.total_tokens == 100


async def test_provider_switch_resets_session(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s1, _ = await mgr.resolve_session(chat_id=1, provider="claude")
    await _simulate_cli_response(mgr, s1, "claude-session-id")

    s2, new2 = await mgr.resolve_session(chat_id=1, provider="codex", model="gpt-5.2-codex")
    assert new2 is True
    assert s2.session_id == ""
    assert s2.provider == "codex"
    assert s2.model == "gpt-5.2-codex"


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
    assert s.model == "opus"
    assert s.message_count == 0
    assert s.total_cost_usd == 0.0
    assert s.total_tokens == 0


async def test_model_update_without_provider_switch(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s1, _ = await mgr.resolve_session(chat_id=1, provider="codex", model="gpt-5.1-codex-mini")
    await _simulate_cli_response(mgr, s1, "codex-session-id")

    s2, is_new = await mgr.resolve_session(chat_id=1, provider="codex", model="gpt-5.2-codex")
    assert is_new is False
    assert s2.session_id == "codex-session-id"
    assert s2.provider == "codex"
    assert s2.model == "gpt-5.2-codex"


async def test_legacy_session_without_model_is_migrated_on_resolve(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text(
        '{"1":{"session_id":"legacy-sid","chat_id":1,"provider":"codex"}}',
        encoding="utf-8",
    )

    mgr = _make_manager(tmp_path, idle_timeout_minutes=30)
    s1, is_new = await mgr.resolve_session(chat_id=1, provider="codex", model="gpt-5.2-codex")
    assert is_new is False
    assert s1.session_id == "legacy-sid"
    assert s1.provider == "codex"
    assert s1.model == "gpt-5.2-codex"

    persisted = path.read_text(encoding="utf-8")
    assert '"model": "gpt-5.2-codex"' in persisted


async def test_sync_session_target_migrates_missing_model_without_value_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.json"
    path.write_text(
        '{"1":{"session_id":"legacy-sid","chat_id":1,"provider":"claude"}}',
        encoding="utf-8",
    )

    mgr = _make_manager(tmp_path)
    session = await mgr.get_active(1)
    assert session is not None
    assert session.model == "opus"

    await mgr.sync_session_target(session, provider="claude", model="opus")
    persisted = path.read_text(encoding="utf-8")
    assert '"model": "opus"' in persisted


async def test_sync_session_target_does_not_overwrite_metrics_from_stale_snapshot(
    tmp_path: Path,
) -> None:
    """sync_session_target must merge target fields without resetting counters."""
    mgr = _make_manager(tmp_path)
    base, _ = await mgr.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr, base, "sess-id")

    fresh = await mgr.get_active(1)
    stale = await mgr.get_active(1)
    assert fresh is not None
    assert stale is not None

    await mgr.update_session(fresh, cost_usd=0.02, tokens=20)
    before_sync = await mgr.get_active(1)
    assert before_sync is not None
    assert before_sync.message_count == 2

    await mgr.sync_session_target(stale, provider="claude", model="opus")
    after_sync = await mgr.get_active(1)
    assert after_sync is not None
    assert after_sync.message_count == 2
    assert after_sync.total_cost_usd == pytest.approx(0.02)
    assert after_sync.total_tokens == 20


async def test_update_session_uses_latest_persisted_counters_from_stale_snapshot(
    tmp_path: Path,
) -> None:
    """update_session should not drop increments when called with stale snapshots."""
    mgr = _make_manager(tmp_path)
    base, _ = await mgr.resolve_session(chat_id=1)
    await _simulate_cli_response(mgr, base, "sess-id")

    stale_a = await mgr.get_active(1)
    stale_b = await mgr.get_active(1)
    assert stale_a is not None
    assert stale_b is not None

    await mgr.update_session(stale_a, cost_usd=0.01, tokens=10)
    await mgr.update_session(stale_b, cost_usd=0.02, tokens=20)

    final = await mgr.get_active(1)
    assert final is not None
    assert final.message_count == 3
    assert final.total_cost_usd == pytest.approx(0.03)
    assert final.total_tokens == 30


async def test_separate_chat_ids(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s1, n1 = await mgr.resolve_session(chat_id=1)
    s2, n2 = await mgr.resolve_session(chat_id=2)
    assert n1 is True
    assert n2 is True
    assert s1.chat_id != s2.chat_id
