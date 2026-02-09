"""Tests for CLAUDE.md / AGENTS.md recursive synchronization."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

from ductor_bot.workspace.init import sync_rule_files, watch_rule_files


def test_sync_claude_to_agents_created(tmp_path: Path) -> None:
    """CLAUDE.md exists, AGENTS.md missing -> AGENTS.md created."""
    (tmp_path / "CLAUDE.md").write_text("# Rules")
    sync_rule_files(tmp_path)
    assert (tmp_path / "AGENTS.md").read_text() == "# Rules"


def test_sync_agents_to_claude_created(tmp_path: Path) -> None:
    """AGENTS.md exists, CLAUDE.md missing -> CLAUDE.md created."""
    (tmp_path / "AGENTS.md").write_text("# Codex Rules")
    sync_rule_files(tmp_path)
    assert (tmp_path / "CLAUDE.md").read_text() == "# Codex Rules"


def test_sync_newer_claude_overwrites_agents(tmp_path: Path) -> None:
    """CLAUDE.md is newer -> overwrites AGENTS.md."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("old content")

    # Ensure mtime difference
    time.sleep(0.05)

    claude = tmp_path / "CLAUDE.md"
    claude.write_text("new content")

    sync_rule_files(tmp_path)
    assert agents.read_text() == "new content"


def test_sync_newer_agents_overwrites_claude(tmp_path: Path) -> None:
    """AGENTS.md is newer -> overwrites CLAUDE.md."""
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("old content")

    time.sleep(0.05)

    agents = tmp_path / "AGENTS.md"
    agents.write_text("new content")

    sync_rule_files(tmp_path)
    assert claude.read_text() == "new content"


def test_sync_identical_no_write(tmp_path: Path) -> None:
    """Both exist with same mtime -> no writes happen."""
    claude = tmp_path / "CLAUDE.md"
    agents = tmp_path / "AGENTS.md"
    claude.write_text("same content")
    agents.write_text("same content")

    # Set same mtime
    mtime = claude.stat().st_mtime
    os.utime(agents, (mtime, mtime))

    stat_before = agents.stat().st_mtime
    sync_rule_files(tmp_path)
    stat_after = agents.stat().st_mtime
    assert stat_before == stat_after


def test_sync_recursive_subdirs(tmp_path: Path) -> None:
    """Sync works across nested subdirectories."""
    sub1 = tmp_path / "sub1"
    sub2 = tmp_path / "sub1" / "sub2"
    sub2.mkdir(parents=True)

    (sub1 / "CLAUDE.md").write_text("# Sub1 Rules")
    (sub2 / "CLAUDE.md").write_text("# Sub2 Rules")

    sync_rule_files(tmp_path)

    assert (sub1 / "AGENTS.md").read_text() == "# Sub1 Rules"
    assert (sub2 / "AGENTS.md").read_text() == "# Sub2 Rules"


def test_sync_skips_venv_and_dotdirs(tmp_path: Path) -> None:
    """Directories like .venv, .git, __pycache__ are skipped."""
    venv_dir = tmp_path / ".venv"
    venv_dir.mkdir()
    (venv_dir / "CLAUDE.md").write_text("should not sync")

    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "CLAUDE.md").write_text("should not sync")

    sync_rule_files(tmp_path)

    assert not (venv_dir / "AGENTS.md").exists()
    assert not (pycache / "AGENTS.md").exists()


# -- watch_rule_files (async watcher) --


async def test_watch_rule_files_syncs_after_change(tmp_path: Path) -> None:
    """Watcher picks up a new CLAUDE.md and creates AGENTS.md."""
    task = asyncio.create_task(watch_rule_files(tmp_path, interval=0.1))
    try:
        # Write CLAUDE.md while watcher is running
        (tmp_path / "CLAUDE.md").write_text("# Synced")
        # Give watcher time to fire
        await asyncio.sleep(0.3)
        assert (tmp_path / "AGENTS.md").read_text() == "# Synced"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_watch_rule_files_syncs_subdirs(tmp_path: Path) -> None:
    """Watcher syncs CLAUDE.md -> AGENTS.md in subdirectories."""
    sub = tmp_path / "cron_tasks" / "daily"
    sub.mkdir(parents=True)

    task = asyncio.create_task(watch_rule_files(tmp_path, interval=0.1))
    try:
        (sub / "AGENTS.md").write_text("# Updated by Codex")
        await asyncio.sleep(0.3)
        assert (sub / "CLAUDE.md").read_text() == "# Updated by Codex"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
