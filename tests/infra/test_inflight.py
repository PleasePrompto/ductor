"""Tests for inflight turn tracking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ductor_bot.infra.inflight import InflightTracker, InflightTurn


def _make_turn(
    chat_id: str = "100",
    *,
    provider: str = "claude",
    model: str = "opus",
    session_id: str = "sess-1",
    prompt_preview: str = "hello world",
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


class TestInflightTrackerLifecycle:
    def test_begin_creates_file(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id="100"))
        assert (tmp_path / "inflight.json").exists()

    def test_complete_removes_entry(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id="100"))
        tracker.complete("100")
        result = tracker.load_interrupted(max_age_seconds=9999)
        assert result == []

    def test_begin_complete_roundtrip(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        turn = _make_turn(chat_id="100", prompt_preview="test prompt")
        tracker.begin(turn)
        interrupted = tracker.load_interrupted(max_age_seconds=9999)
        assert len(interrupted) == 1
        assert interrupted[0].chat_id == "100"
        assert interrupted[0].prompt_preview == "test prompt"


class TestMultipleChatIds:
    def test_independent_tracking(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id="100"))
        tracker.begin(_make_turn(chat_id="200"))
        interrupted = tracker.load_interrupted(max_age_seconds=9999)
        assert len(interrupted) == 2
        chat_ids = {t.chat_id for t in interrupted}
        assert chat_ids == {"100", "200"}

    def test_complete_one_keeps_other(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id="100"))
        tracker.begin(_make_turn(chat_id="200"))
        tracker.complete("100")
        interrupted = tracker.load_interrupted(max_age_seconds=9999)
        assert len(interrupted) == 1
        assert interrupted[0].chat_id == "200"


class TestSafetyFilters:
    def test_is_recovery_skipped(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id="100", is_recovery=True))
        interrupted = tracker.load_interrupted(max_age_seconds=9999)
        assert interrupted == []

    def test_old_entries_skipped(self, tmp_path: Path) -> None:
        old_time = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id="100", started_at=old_time))
        interrupted = tracker.load_interrupted(max_age_seconds=3600)
        assert interrupted == []

    def test_recent_entry_kept(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id="100"))
        interrupted = tracker.load_interrupted(max_age_seconds=3600)
        assert len(interrupted) == 1


class TestEdgeCases:
    def test_no_file_returns_empty(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        assert tracker.load_interrupted(max_age_seconds=9999) == []

    def test_corrupt_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "inflight.json"
        path.write_text("not json {{{")
        tracker = InflightTracker(path)
        assert tracker.load_interrupted(max_age_seconds=9999) == []

    def test_clear_removes_file(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id="100"))
        tracker.clear()
        assert not (tmp_path / "inflight.json").exists()

    def test_clear_nonexistent_no_error(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.clear()  # Should not raise

    def test_complete_nonexistent_chat_no_error(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.complete("999")  # Should not raise

    def test_begin_overwrites_same_chat_id(self, tmp_path: Path) -> None:
        tracker = InflightTracker(tmp_path / "inflight.json")
        tracker.begin(_make_turn(chat_id="100", prompt_preview="first"))
        tracker.begin(_make_turn(chat_id="100", prompt_preview="second"))
        interrupted = tracker.load_interrupted(max_age_seconds=9999)
        assert len(interrupted) == 1
        assert interrupted[0].prompt_preview == "second"
