"""Tests for the recovery planner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ductor_bot.infra.inflight import InflightTracker, InflightTurn
from ductor_bot.infra.recovery import RecoveryAction, RecoveryPlanner


def _make_turn(
    chat_id: int = 100,
    *,
    provider: str = "claude",
    model: str = "opus",
    session_id: str = "sess-1",
    prompt_preview: str = "hello",
    started_at: str | None = None,
    is_recovery: bool = False,
    path: str = "normal",
) -> InflightTurn:
    return InflightTurn(
        chat_id=chat_id,
        provider=provider,
        model=model,
        session_id=session_id,
        prompt_preview=prompt_preview,
        started_at=started_at or datetime.now(UTC).isoformat(),
        is_recovery=is_recovery,
        path=path,
    )


class TestRecoveryPlannerForeground:
    def test_finds_foreground_interrupt(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id=100, prompt_preview="fix the bug"))
        planner = RecoveryPlanner(
            inflight=tracker,
            named_sessions=[],
            max_age_seconds=9999,
        )
        actions = planner.plan()
        assert len(actions) == 1
        assert actions[0].kind == "foreground"
        assert actions[0].chat_id == 100
        assert actions[0].prompt_preview == "fix the bug"

    def test_skips_recovery_turns(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id=100, is_recovery=True))
        planner = RecoveryPlanner(inflight=tracker, named_sessions=[], max_age_seconds=9999)
        assert planner.plan() == []

    def test_skips_old_turns(self, tmp_path: Path) -> None:
        old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id=100, started_at=old))
        planner = RecoveryPlanner(inflight=tracker, named_sessions=[], max_age_seconds=3600)
        assert planner.plan() == []

    def test_session_id_preserved(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id=100, session_id="abc-123"))
        planner = RecoveryPlanner(inflight=tracker, named_sessions=[], max_age_seconds=9999)
        actions = planner.plan()
        assert actions[0].session_id == "abc-123"


class TestRecoveryPlannerNamedSessions:
    def test_finds_recovered_named_session(self, tmp_path: Path) -> None:
        """Named sessions with status 'idle' and non-empty prompt_preview are candidates."""
        from ductor_bot.session.named import NamedSession

        ns = NamedSession(
            name="boldowl",
            chat_id=100,
            provider="claude",
            model="opus",
            session_id="sess-ns-1",
            prompt_preview="build the feature",
            status="idle",
            created_at=1000.0,
            message_count=1,
        )
        tracker = InflightTracker(tmp_path / "inflight.json")
        planner = RecoveryPlanner(
            inflight=tracker,
            named_sessions=[ns],
            max_age_seconds=9999,
        )
        actions = planner.plan()
        ns_actions = [a for a in actions if a.kind == "named_session"]
        assert len(ns_actions) == 1
        assert ns_actions[0].session_name == "boldowl"
        assert ns_actions[0].session_id == "sess-ns-1"

    @pytest.mark.parametrize(
        "name", ["ia-research", "ia.main.t3892.xf47692", "ia.main.t10.x255106fd"]
    )
    def test_skips_inter_agent_sessions(self, tmp_path: Path, name: str) -> None:
        from ductor_bot.session.named import NamedSession

        ns = NamedSession(
            name=name,
            chat_id=100,
            provider="claude",
            model="opus",
            session_id="sess-ia-1",
            prompt_preview="research task",
            status="idle",
            created_at=1000.0,
        )
        tracker = InflightTracker(tmp_path / "inflight.json")
        planner = RecoveryPlanner(inflight=tracker, named_sessions=[ns], max_age_seconds=9999)
        assert planner.plan() == []

    def test_skips_ended_sessions(self, tmp_path: Path) -> None:
        from ductor_bot.session.named import NamedSession

        ns = NamedSession(
            name="boldowl",
            chat_id=100,
            provider="claude",
            model="opus",
            session_id="",
            prompt_preview="done task",
            status="ended",
            created_at=1000.0,
        )
        tracker = InflightTracker(tmp_path / "inflight.json")
        planner = RecoveryPlanner(inflight=tracker, named_sessions=[ns], max_age_seconds=9999)
        assert planner.plan() == []

    def test_skips_running_sessions(self, tmp_path: Path) -> None:
        """Running sessions should not be recovered (they're still active)."""
        from ductor_bot.session.named import NamedSession

        ns = NamedSession(
            name="boldowl",
            chat_id=100,
            provider="claude",
            model="opus",
            session_id="sess-1",
            prompt_preview="active task",
            status="running",
            created_at=1000.0,
        )
        tracker = InflightTracker(tmp_path / "inflight.json")
        planner = RecoveryPlanner(inflight=tracker, named_sessions=[ns], max_age_seconds=9999)
        assert planner.plan() == []

    def test_skips_sessions_without_session_id(self, tmp_path: Path) -> None:
        """Sessions that never got a CLI session_id can't be resumed."""
        from ductor_bot.session.named import NamedSession

        ns = NamedSession(
            name="boldowl",
            chat_id=100,
            provider="claude",
            model="opus",
            session_id="",
            prompt_preview="never started",
            status="idle",
            created_at=1000.0,
        )
        tracker = InflightTracker(tmp_path / "inflight.json")
        planner = RecoveryPlanner(inflight=tracker, named_sessions=[ns], max_age_seconds=9999)
        assert planner.plan() == []


class TestRecoveryPlannerMixed:
    def test_foreground_and_named_combined(self, tmp_path: Path) -> None:
        from ductor_bot.session.named import NamedSession

        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id=100, prompt_preview="fg task"))
        ns = NamedSession(
            name="boldowl",
            chat_id=200,
            provider="codex",
            model="gpt-4",
            session_id="sess-ns-1",
            prompt_preview="bg task",
            status="idle",
            created_at=1000.0,
            message_count=1,
        )
        planner = RecoveryPlanner(
            inflight=tracker,
            named_sessions=[ns],
            max_age_seconds=9999,
        )
        actions = planner.plan()
        assert len(actions) == 2
        kinds = {a.kind for a in actions}
        assert kinds == {"foreground", "named_session"}

    def test_empty_state_returns_empty(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        planner = RecoveryPlanner(inflight=tracker, named_sessions=[], max_age_seconds=9999)
        assert planner.plan() == []


class TestRecoveryActionDataclass:
    def test_fields(self) -> None:
        action = RecoveryAction(
            chat_id=100,
            kind="foreground",
            provider="claude",
            model="opus",
            session_id="sess-1",
            prompt_preview="test",
            session_name="",
        )
        assert action.chat_id == 100
        assert action.kind == "foreground"
        assert action.session_name == ""
