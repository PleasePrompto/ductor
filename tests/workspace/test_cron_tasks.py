"""Tests for cron task folder CRUD."""

from __future__ import annotations

from pathlib import Path

import pytest

from ductor_bot.workspace.cron_tasks import (
    create_cron_task,
    delete_cron_task,
    list_cron_tasks,
)
from ductor_bot.workspace.paths import DuctorPaths


def _make_paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "fw"
    paths = DuctorPaths(ductor_home=tmp_path / "home", home_defaults=fw / "workspace", framework_root=fw)
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


def test_create_cron_task_agents_md_mirrors_claude_md(tmp_path: Path) -> None:
    """AGENTS.md (Codex rule file) has identical content to CLAUDE.md."""
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "Build the login page")
    claude_md = task_path / "CLAUDE.md"
    agents_md = task_path / "AGENTS.md"
    assert agents_md.exists()
    assert agents_md.read_text() == claude_md.read_text()


def test_create_cron_task_creates_scripts_dir(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "Build the login page")
    assert (task_path / "scripts").is_dir()


def test_create_cron_task_no_venv_by_default(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "desc")
    assert not (task_path / ".venv").exists()


def test_create_cron_task_with_venv(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    task_path = create_cron_task(paths, "my-feature", "My Feature", "desc", with_venv=True)
    venv_dir = task_path / ".venv"
    assert venv_dir.is_dir()
    assert (venv_dir / "bin" / "python").exists() or (venv_dir / "Scripts" / "python.exe").exists()


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
