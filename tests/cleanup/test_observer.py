"""Tests for the file cleanup observer."""

from __future__ import annotations

import time
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

from ductor_bot.cleanup.observer import CleanupObserver, _delete_old_files
from ductor_bot.config import AgentConfig, CleanupConfig
from ductor_bot.workspace.paths import DuctorPaths

# -- _delete_old_files (sync helper) --


def test_delete_old_files_removes_expired(tmp_path: Path) -> None:
    old_file = tmp_path / "old.txt"
    old_file.write_text("old")
    # Backdate mtime by 40 days.
    old_mtime = time.time() - 40 * 86400
    import os

    os.utime(old_file, (old_mtime, old_mtime))

    recent_file = tmp_path / "recent.txt"
    recent_file.write_text("recent")

    deleted = _delete_old_files(tmp_path, max_age_days=30)

    assert deleted == 1
    assert not old_file.exists()
    assert recent_file.exists()


def test_delete_old_files_skips_subdirectories(tmp_path: Path) -> None:
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    old_mtime = time.time() - 40 * 86400
    import os

    os.utime(subdir, (old_mtime, old_mtime))

    deleted = _delete_old_files(tmp_path, max_age_days=30)
    assert deleted == 0
    assert subdir.is_dir()


def test_delete_old_files_nonexistent_dir(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert _delete_old_files(missing, max_age_days=30) == 0


def test_delete_old_files_empty_dir(tmp_path: Path) -> None:
    assert _delete_old_files(tmp_path, max_age_days=30) == 0


def test_delete_old_files_all_recent(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    assert _delete_old_files(tmp_path, max_age_days=30) == 0


# -- CleanupObserver --


def _make_config(*, enabled: bool = True, check_hour: int = 3) -> AgentConfig:
    return AgentConfig(
        cleanup=CleanupConfig(
            enabled=enabled,
            telegram_files_days=30,
            output_to_user_days=30,
            check_hour=check_hour,
        ),
    )


def _make_paths(tmp_path: Path) -> DuctorPaths:
    return DuctorPaths(ductor_home=tmp_path)


async def test_start_disabled_does_not_spawn_task(tmp_path: Path) -> None:
    config = _make_config(enabled=False)
    observer = CleanupObserver(config, _make_paths(tmp_path))
    await observer.start()
    assert observer._task is None
    await observer.stop()


async def test_start_and_stop(tmp_path: Path) -> None:
    config = _make_config()
    observer = CleanupObserver(config, _make_paths(tmp_path))
    await observer.start()
    assert observer._task is not None
    assert observer._running
    await observer.stop()
    assert not observer._running


async def test_execute_deletes_files(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.telegram_files_dir.mkdir(parents=True, exist_ok=True)
    paths.output_to_user_dir.mkdir(parents=True, exist_ok=True)

    old_tg = paths.telegram_files_dir / "old_photo.jpg"
    old_tg.write_text("photo")
    old_out = paths.output_to_user_dir / "old_report.pdf"
    old_out.write_text("report")

    import os

    old_mtime = time.time() - 40 * 86400
    os.utime(old_tg, (old_mtime, old_mtime))
    os.utime(old_out, (old_mtime, old_mtime))

    recent_tg = paths.telegram_files_dir / "new.jpg"
    recent_tg.write_text("new")

    config = _make_config()
    observer = CleanupObserver(config, paths)
    await observer._execute()

    assert not old_tg.exists()
    assert not old_out.exists()
    assert recent_tg.exists()


async def test_maybe_run_skips_wrong_hour(tmp_path: Path) -> None:
    config = _make_config(check_hour=3)
    observer = CleanupObserver(config, _make_paths(tmp_path))

    from datetime import datetime

    fake_now = datetime(2025, 6, 1, 10, 0, tzinfo=UTC)
    with patch("ductor_bot.cleanup.observer.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)  # noqa: DTZ001, PLW0108
        await observer._maybe_run()

    assert observer._last_run_date == ""


async def test_maybe_run_skips_duplicate_same_day(tmp_path: Path) -> None:
    config = _make_config(check_hour=3)
    paths = _make_paths(tmp_path)
    paths.telegram_files_dir.mkdir(parents=True, exist_ok=True)
    paths.output_to_user_dir.mkdir(parents=True, exist_ok=True)
    observer = CleanupObserver(config, paths)

    from datetime import datetime

    fake_now = datetime(2025, 6, 1, 3, 30, tzinfo=UTC)
    with patch("ductor_bot.cleanup.observer.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)  # noqa: DTZ001, PLW0108
        await observer._maybe_run()
        assert observer._last_run_date == "2025-06-01"

        # Second call same day: should not run again.
        await observer._maybe_run()
        assert observer._last_run_date == "2025-06-01"
