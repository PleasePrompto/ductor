"""Tests for cron task folder CRUD."""

from __future__ import annotations

from pathlib import Path

import pytest

from ductor_bot.workspace.cron_tasks import (
    create_cron_task,
    delete_cron_task,
    ensure_task_rule_files,
    list_cron_tasks,
)
from ductor_bot.workspace.paths import DuctorPaths


def _make_paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "fw"
    paths = DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw / "workspace", framework_root=fw
    )
    paths.cron_tasks_dir.mkdir(parents=True)
    return paths


# -- create_cron_task --


def test_create_cron_task_creates_directory(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "Build the login page")
    assert task_path.is_dir()
    assert task_path.name == "my-feature"


def test_create_cron_task_creates_fixed_claude_md(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "Build the login page")
    claude_md = task_path / "CLAUDE.md"
    assert claude_md.exists()
    content = claude_md.read_text()
    assert "Your Mission" in content
    assert "TASK_DESCRIPTION.md" in content
    assert "automated agent" in content
    # Description should NOT be in CLAUDE.md
    assert "Build the login page" not in content


def test_create_cron_task_creates_task_description(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "Build the login page")
    task_desc = task_path / "TASK_DESCRIPTION.md"
    assert task_desc.exists()
    content = task_desc.read_text()
    assert "My Feature" in content
    assert "Build the login page" in content
    assert "## Assignment" in content
    assert "## Output" in content


def test_create_cron_task_creates_memory_md(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "Build the login page")
    memory_md = task_path / "my-feature_MEMORY.md"
    assert memory_md.exists()


def test_create_cron_task_only_claude_md_when_no_parent_rules(tmp_path: Path) -> None:
    """Without parent rule files, only CLAUDE.md is created (fallback)."""
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "Build the login page")
    assert (task_path / "CLAUDE.md").exists()
    assert not (task_path / "AGENTS.md").exists()
    assert not (task_path / "GEMINI.md").exists()


def test_create_cron_task_mirrors_parent_rule_files(tmp_path: Path) -> None:
    """Task folder mirrors whichever rule files exist in the parent cron_tasks/ dir."""
    paths = _make_paths(tmp_path)
    # Simulate RulesSelector having deployed all three providers
    (paths.cron_tasks_dir / "CLAUDE.md").write_text("parent")
    (paths.cron_tasks_dir / "AGENTS.md").write_text("parent")
    (paths.cron_tasks_dir / "GEMINI.md").write_text("parent")
    task_path = create_cron_task(paths, "my-feature", "My Feature", "Build the login page")
    claude_md = task_path / "CLAUDE.md"
    agents_md = task_path / "AGENTS.md"
    gemini_md = task_path / "GEMINI.md"
    assert claude_md.exists()
    assert agents_md.exists()
    assert gemini_md.exists()
    assert agents_md.read_text() == claude_md.read_text()
    assert gemini_md.read_text() == claude_md.read_text()


def test_create_cron_task_only_gemini_when_parent_has_gemini(tmp_path: Path) -> None:
    """When only Gemini is authenticated, only GEMINI.md is created."""
    paths = _make_paths(tmp_path)
    (paths.cron_tasks_dir / "GEMINI.md").write_text("parent")
    task_path = create_cron_task(paths, "my-feature", "My Feature", "desc")
    assert not (task_path / "CLAUDE.md").exists()
    assert not (task_path / "AGENTS.md").exists()
    assert (task_path / "GEMINI.md").exists()


def test_create_cron_task_claude_and_codex_from_parent(tmp_path: Path) -> None:
    """When parent has CLAUDE.md + AGENTS.md, both are created in task folder."""
    paths = _make_paths(tmp_path)
    (paths.cron_tasks_dir / "CLAUDE.md").write_text("parent")
    (paths.cron_tasks_dir / "AGENTS.md").write_text("parent")
    task_path = create_cron_task(paths, "my-feature", "My Feature", "desc")
    assert (task_path / "CLAUDE.md").exists()
    assert (task_path / "AGENTS.md").exists()
    assert not (task_path / "GEMINI.md").exists()


def test_create_cron_task_creates_scripts_dir(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "Build the login page")
    assert (task_path / "scripts").is_dir()


def test_create_cron_task_no_venv_by_default(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "desc")
    assert not (task_path / ".venv").exists()


def test_create_cron_task_with_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_create(env_dir: Path, *, with_pip: bool = False) -> None:  # noqa: ARG001
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "bin").mkdir()
        (env_dir / "bin" / "python").touch()

    monkeypatch.setattr("venv.create", fake_create)
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "desc", with_venv=True)
    venv_dir = task_path / ".venv"
    assert venv_dir.is_dir()
    assert (venv_dir / "bin" / "python").exists()


def test_create_cron_task_duplicate_raises(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    create_cron_task(paths, "my-feature", "My Feature", "desc")
    with pytest.raises(FileExistsError):
        create_cron_task(paths, "my-feature", "My Feature", "desc")


def test_create_cron_task_sanitizes_name(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "My Feature!!", "My Feature", "desc")
    assert task_path.name == "my-feature"


def test_create_cron_task_rejects_empty_name(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    with pytest.raises(ValueError, match="name"):
        create_cron_task(paths, "", "Title", "desc")


def test_create_cron_task_rejects_path_traversal(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    with pytest.raises(ValueError, match="name"):
        create_cron_task(paths, "../escape", "Title", "desc")


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
    task_path = create_cron_task(paths, "complete", "Complete", "desc")
    assert (task_path / "CLAUDE.md").exists()
    assert (task_path / "AGENTS.md").exists()

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


# -- list_cron_tasks --


def test_list_cron_tasks_empty(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    assert list_cron_tasks(paths) == []


def test_list_cron_tasks_returns_names(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    create_cron_task(paths, "alpha", "Alpha", "desc")
    create_cron_task(paths, "beta", "Beta", "desc")
    tasks = list_cron_tasks(paths)
    assert sorted(tasks) == ["alpha", "beta"]


def test_list_cron_tasks_ignores_files(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    (paths.cron_tasks_dir / "not-a-task.txt").write_text("noise")
    assert list_cron_tasks(paths) == []


# -- delete_cron_task --


def test_delete_cron_task(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    create_cron_task(paths, "my-feature", "My Feature", "desc")
    assert delete_cron_task(paths, "my-feature") is True
    assert list_cron_tasks(paths) == []


def test_delete_cron_task_nonexistent(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    assert delete_cron_task(paths, "missing") is False


def test_delete_cron_task_removes_all_contents(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "desc")
    (task_path / "extra.txt").write_text("extra content")
    delete_cron_task(paths, "my-feature")
    assert not task_path.exists()
