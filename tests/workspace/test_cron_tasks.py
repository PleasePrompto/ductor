"""Tests for cron task rule file sync."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.workspace.cron_tasks import ensure_task_rule_files
from ductor_bot.workspace.paths import DuctorPaths


def _make_paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "fw"
    paths = DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw / "workspace", framework_root=fw
    )
    paths.cron_tasks_dir.mkdir(parents=True)
    return paths


# -- ensure_task_rule_files --


def test_ensure_adds_missing_gemini_md(tmp_path: Path) -> None:
    """Existing task with CLAUDE.md + AGENTS.md gets GEMINI.md when parent has it."""
    paths = _make_paths(tmp_path)
    task_dir = paths.cron_tasks_dir / "old-task"
    task_dir.mkdir()
    (task_dir / "CLAUDE.md").write_text("rule content")
    (task_dir / "AGENTS.md").write_text("rule content")

    # Simulate Gemini getting authenticated (parent now has GEMINI.md)
    (paths.cron_tasks_dir / "CLAUDE.md").write_text("parent")
    (paths.cron_tasks_dir / "AGENTS.md").write_text("parent")
    (paths.cron_tasks_dir / "GEMINI.md").write_text("parent")

    created = ensure_task_rule_files(paths.cron_tasks_dir)
    assert created == 1
    assert (task_dir / "GEMINI.md").exists()
    assert (task_dir / "GEMINI.md").read_text() == "rule content"


def test_ensure_adds_multiple_missing_files(tmp_path: Path) -> None:
    """Task with only CLAUDE.md gets AGENTS.md + GEMINI.md when parent has all three."""
    paths = _make_paths(tmp_path)
    task_dir = paths.cron_tasks_dir / "legacy-task"
    task_dir.mkdir()
    (task_dir / "CLAUDE.md").write_text("rule content")

    (paths.cron_tasks_dir / "CLAUDE.md").write_text("parent")
    (paths.cron_tasks_dir / "AGENTS.md").write_text("parent")
    (paths.cron_tasks_dir / "GEMINI.md").write_text("parent")

    created = ensure_task_rule_files(paths.cron_tasks_dir)
    assert created == 2
    assert (task_dir / "AGENTS.md").read_text() == "rule content"
    assert (task_dir / "GEMINI.md").read_text() == "rule content"


def test_ensure_noop_when_all_present(tmp_path: Path) -> None:
    """No files created when task already has all expected rule files."""
    paths = _make_paths(tmp_path)
    (paths.cron_tasks_dir / "CLAUDE.md").write_text("parent")
    (paths.cron_tasks_dir / "AGENTS.md").write_text("parent")
    task_dir = paths.cron_tasks_dir / "complete"
    task_dir.mkdir()
    (task_dir / "CLAUDE.md").write_text("rule content")
    (task_dir / "AGENTS.md").write_text("rule content")

    created = ensure_task_rule_files(paths.cron_tasks_dir)
    assert created == 0


def test_ensure_never_removes_files(tmp_path: Path) -> None:
    """Rule files are never removed, even when parent no longer has them."""
    paths = _make_paths(tmp_path)
    task_dir = paths.cron_tasks_dir / "old-task"
    task_dir.mkdir()
    (task_dir / "CLAUDE.md").write_text("rule content")
    (task_dir / "AGENTS.md").write_text("rule content")
    (task_dir / "GEMINI.md").write_text("rule content")

    # Parent only has CLAUDE.md (Codex + Gemini de-authenticated)
    (paths.cron_tasks_dir / "CLAUDE.md").write_text("parent")

    ensure_task_rule_files(paths.cron_tasks_dir)
    # All three still exist in task folder
    assert (task_dir / "CLAUDE.md").exists()
    assert (task_dir / "AGENTS.md").exists()
    assert (task_dir / "GEMINI.md").exists()


def test_ensure_skips_non_task_dirs(tmp_path: Path) -> None:
    """Directories without any rule files are skipped (not task folders)."""
    paths = _make_paths(tmp_path)
    (paths.cron_tasks_dir / "CLAUDE.md").write_text("parent")
    (paths.cron_tasks_dir / "GEMINI.md").write_text("parent")

    # Create a non-task directory (e.g. scripts helper)
    (paths.cron_tasks_dir / "random-dir").mkdir()
    (paths.cron_tasks_dir / "random-dir" / "helper.py").write_text("pass")

    created = ensure_task_rule_files(paths.cron_tasks_dir)
    assert created == 0
    assert not (paths.cron_tasks_dir / "random-dir" / "CLAUDE.md").exists()


def test_ensure_idempotent(tmp_path: Path) -> None:
    """Calling ensure twice produces the same result."""
    paths = _make_paths(tmp_path)
    task_dir = paths.cron_tasks_dir / "my-task"
    task_dir.mkdir()
    (task_dir / "CLAUDE.md").write_text("rule content")
    (paths.cron_tasks_dir / "CLAUDE.md").write_text("parent")
    (paths.cron_tasks_dir / "GEMINI.md").write_text("parent")

    first = ensure_task_rule_files(paths.cron_tasks_dir)
    second = ensure_task_rule_files(paths.cron_tasks_dir)
    assert first == 1
    assert second == 0
