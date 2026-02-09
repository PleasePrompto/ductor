"""Tests for workspace file reader."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.workspace.loader import read_file, read_mainmemory
from ductor_bot.workspace.paths import DuctorPaths


def _make_paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "fw"
    return DuctorPaths(ductor_home=tmp_path / "home", home_defaults=fw / "workspace", framework_root=fw)


# -- read_file --


def test_read_existing_file(tmp_path: Path) -> None:
    f = tmp_path / "test.md"
    f.write_text("Hello world")
    assert read_file(f) == "Hello world"


def test_read_nonexistent_file(tmp_path: Path) -> None:
    assert read_file(tmp_path / "missing.md") is None


def test_read_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "empty.md"
    f.write_text("")
    assert read_file(f) == ""


# -- read_mainmemory --


def test_read_mainmemory_exists(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.memory_system_dir.mkdir(parents=True)
    paths.mainmemory_path.write_text("# Memories\n- Learned X")
    result = read_mainmemory(paths)
    assert result == "# Memories\n- Learned X"


def test_read_mainmemory_missing(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    assert read_mainmemory(paths) == ""
