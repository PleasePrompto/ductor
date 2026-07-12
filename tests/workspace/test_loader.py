"""Tests for workspace file reader."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.workspace.loader import (
    build_appended_files_block,
    read_file,
    read_mainmemory,
)
from ductor_bot.workspace.paths import DuctorPaths


def _make_paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "fw"
    return DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw / "workspace", framework_root=fw
    )


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


# -- build_appended_files_block --


def _ws(tmp_path: Path) -> DuctorPaths:
    paths = _make_paths(tmp_path)
    paths.workspace.mkdir(parents=True, exist_ok=True)
    return paths


async def test_appended_block_concatenates_existing(tmp_path: Path) -> None:
    paths = _ws(tmp_path)
    (paths.workspace / "A.md").write_text("alpha")
    (paths.workspace / "B.md").write_text("beta")
    block = await build_appended_files_block(paths, ["A.md", "B.md"])
    assert block == "alpha\n\nbeta"


async def test_appended_block_empty_list_is_none(tmp_path: Path) -> None:
    paths = _ws(tmp_path)
    assert await build_appended_files_block(paths, []) is None


async def test_appended_block_all_missing_is_none(tmp_path: Path) -> None:
    paths = _ws(tmp_path)
    assert await build_appended_files_block(paths, ["nope.md", "gone.md"]) is None


async def test_appended_block_skips_missing_without_error(tmp_path: Path) -> None:
    paths = _ws(tmp_path)
    (paths.workspace / "A.md").write_text("alpha")
    block = await build_appended_files_block(paths, ["A.md", "missing.md"])
    assert block == "alpha"


async def test_appended_block_skips_blank_files(tmp_path: Path) -> None:
    paths = _ws(tmp_path)
    (paths.workspace / "A.md").write_text("   \n  ")
    (paths.workspace / "B.md").write_text("beta")
    block = await build_appended_files_block(paths, ["A.md", "B.md"])
    assert block == "beta"


async def test_appended_block_skips_oversized_file(tmp_path: Path) -> None:
    paths = _ws(tmp_path)
    (paths.workspace / "huge.md").write_text("x" * (256 * 1024 + 1))
    (paths.workspace / "small.md").write_text("small")
    block = await build_appended_files_block(paths, ["huge.md", "small.md"])
    assert block == "small"


async def test_appended_block_blocks_absolute_path(tmp_path: Path) -> None:
    paths = _ws(tmp_path)
    secret = tmp_path / "secret.md"
    secret.write_text("top secret")
    block = await build_appended_files_block(paths, [str(secret)])
    assert block is None


async def test_appended_block_blocks_dotdot_escape(tmp_path: Path) -> None:
    paths = _ws(tmp_path)
    (paths.ductor_home / "outside.md").write_text("outside")
    block = await build_appended_files_block(paths, ["../outside.md"])
    assert block is None


async def test_appended_block_blocks_symlink_escape(tmp_path: Path) -> None:
    paths = _ws(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    link = paths.workspace / "link.md"
    link.symlink_to(outside)
    block = await build_appended_files_block(paths, ["link.md"])
    assert block is None
