"""Tests for PID lockfile management."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


class TestIsProcessAlive:
    """Test process liveness detection."""

    def test_current_process_is_alive(self) -> None:
        from ductor_bot.infra.pidlock import _is_process_alive

        assert _is_process_alive(os.getpid()) is True

    def test_nonexistent_pid_is_dead(self) -> None:
        from ductor_bot.infra.pidlock import _is_process_alive

        # PID 2^30 is extremely unlikely to exist
        assert _is_process_alive(2**30) is False

    def test_permission_error_means_alive(self) -> None:
        from ductor_bot.infra.pidlock import _is_process_alive

        with patch("os.kill", side_effect=PermissionError):
            assert _is_process_alive(999) is True

    def test_windows_winerror_87_is_dead(self) -> None:
        """Test that WinError 87 (invalid parameter) is treated as process dead on Windows."""
        from ductor_bot.infra.pidlock import _is_process_alive

        # Mock an OSError with winerror=87
        err = OSError()
        err.winerror = 87

        with (
            patch("ductor_bot.infra.pidlock._IS_WINDOWS", True),
            patch("os.kill", side_effect=err),
        ):
            assert _is_process_alive(999999) is False

    def test_windows_self_check_bypass(self) -> None:
        """Test that checking our own PID returns True without calling os.kill on Windows."""
        from ductor_bot.infra.pidlock import _is_process_alive

        with (
            patch("ductor_bot.infra.pidlock._IS_WINDOWS", True),
            patch("os.kill") as mock_kill,
        ):
            assert _is_process_alive(os.getpid()) is True
            # Should NOT have called os.kill because of the bypass logic
            mock_kill.assert_not_called()


class TestAcquireLock:
    """Test PID lock acquisition."""

    def test_creates_pid_file(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock, release_lock

        pid_file = tmp_path / "bot.pid"
        acquire_lock(pid_file=pid_file)
        try:
            assert pid_file.exists()
            assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        finally:
            release_lock(pid_file=pid_file)

    def test_stale_pid_file_overwritten(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock, release_lock

        pid_file = tmp_path / "bot.pid"
        # Write a PID that doesn't exist
        pid_file.write_text("999999999", encoding="utf-8")

        acquire_lock(pid_file=pid_file)
        try:
            assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        finally:
            release_lock(pid_file=pid_file)

    def test_corrupt_pid_file_overwritten(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock, release_lock

        pid_file = tmp_path / "bot.pid"
        pid_file.write_text("not-a-number", encoding="utf-8")

        acquire_lock(pid_file=pid_file)
        try:
            assert pid_file.exists()
        finally:
            release_lock(pid_file=pid_file)

    def test_active_pid_without_kill_raises_system_exit(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock

        pid_file = tmp_path / "bot.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        with pytest.raises(SystemExit):
            # Our own PID is alive, so without kill_existing it should fail
            acquire_lock(pid_file=pid_file)

    def test_active_pid_with_kill_kills_and_acquires(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock, release_lock

        pid_file = tmp_path / "bot.pid"
        fake_pid = 999999999
        pid_file.write_text(str(fake_pid), encoding="utf-8")

        with (
            patch("ductor_bot.infra.pidlock._is_process_alive", return_value=True),
            patch("ductor_bot.infra.pidlock._kill_and_wait"),
        ):
            acquire_lock(pid_file=pid_file, kill_existing=True)

        try:
            assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        finally:
            release_lock(pid_file=pid_file)

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock, release_lock

        pid_file = tmp_path / "deep" / "nested" / "bot.pid"
        acquire_lock(pid_file=pid_file)
        try:
            assert pid_file.exists()
        finally:
            release_lock(pid_file=pid_file)


class TestReleaseLock:
    """Test PID lock release."""

    def test_removes_own_pid_file(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import release_lock

        pid_file = tmp_path / "bot.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        release_lock(pid_file=pid_file)
        assert not pid_file.exists()

    def test_does_not_remove_other_pid(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import release_lock

        pid_file = tmp_path / "bot.pid"
        pid_file.write_text("999999999", encoding="utf-8")
        release_lock(pid_file=pid_file)
        assert pid_file.exists()  # Should NOT be removed

    def test_noop_when_no_file(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import release_lock

        pid_file = tmp_path / "bot.pid"
        release_lock(pid_file=pid_file)  # No error

    def test_removes_corrupt_pid_file(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import release_lock

        pid_file = tmp_path / "bot.pid"
        pid_file.write_text("garbage", encoding="utf-8")
        release_lock(pid_file=pid_file)
        assert not pid_file.exists()
