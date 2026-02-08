"""Tests for restart sentinel and marker management."""

from __future__ import annotations

import json
from pathlib import Path


class TestRestartSentinel:
    """Test sentinel file write/consume for post-restart notifications."""

    def test_write_creates_file(self, tmp_path: Path) -> None:
        from ductor_bot.infra.restart import write_restart_sentinel

        sentinel = tmp_path / "restart-sentinel.json"
        write_restart_sentinel(chat_id=42, message="Done.", sentinel_path=sentinel)
        assert sentinel.exists()
        data = json.loads(sentinel.read_text(encoding="utf-8"))
        assert data["chat_id"] == 42
        assert data["message"] == "Done."
        assert "timestamp" in data

    def test_consume_returns_data_and_deletes(self, tmp_path: Path) -> None:
        from ductor_bot.infra.restart import (
            consume_restart_sentinel,
            write_restart_sentinel,
        )

        sentinel = tmp_path / "restart-sentinel.json"
        write_restart_sentinel(chat_id=7, sentinel_path=sentinel)
        data = consume_restart_sentinel(sentinel_path=sentinel)
        assert data is not None
        assert data["chat_id"] == 7
        assert not sentinel.exists()

    def test_consume_missing_returns_none(self, tmp_path: Path) -> None:
        from ductor_bot.infra.restart import consume_restart_sentinel

        sentinel = tmp_path / "restart-sentinel.json"
        assert consume_restart_sentinel(sentinel_path=sentinel) is None

    def test_consume_corrupt_returns_none(self, tmp_path: Path) -> None:
        from ductor_bot.infra.restart import consume_restart_sentinel

        sentinel = tmp_path / "restart-sentinel.json"
        sentinel.write_text("{invalid json", encoding="utf-8")
        assert consume_restart_sentinel(sentinel_path=sentinel) is None
        assert not sentinel.exists()  # Cleaned up

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        from ductor_bot.infra.restart import write_restart_sentinel

        sentinel = tmp_path / "deep" / "restart-sentinel.json"
        write_restart_sentinel(chat_id=1, sentinel_path=sentinel)
        assert sentinel.exists()


class TestRestartMarker:
    """Test marker file for signaling restart to running bot."""

    def test_write_creates_marker(self, tmp_path: Path) -> None:
        from ductor_bot.infra.restart import write_restart_marker

        marker = tmp_path / "restart-requested"
        write_restart_marker(marker_path=marker)
        assert marker.exists()

    def test_consume_returns_true_and_deletes(self, tmp_path: Path) -> None:
        from ductor_bot.infra.restart import (
            consume_restart_marker,
            write_restart_marker,
        )

        marker = tmp_path / "restart-requested"
        write_restart_marker(marker_path=marker)
        assert consume_restart_marker(marker_path=marker) is True
        assert not marker.exists()

    def test_consume_missing_returns_false(self, tmp_path: Path) -> None:
        from ductor_bot.infra.restart import consume_restart_marker

        marker = tmp_path / "restart-requested"
        assert consume_restart_marker(marker_path=marker) is False


class TestExitRestart:
    """Test the EXIT_RESTART constant."""

    def test_exit_restart_is_42(self) -> None:
        from ductor_bot.infra.restart import EXIT_RESTART

        assert EXIT_RESTART == 42
