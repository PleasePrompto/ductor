"""Keeping agent-written handoffs out of a user's git history.

The failure this guards against is unrecoverable in practice: a working-state
file, rewritten every turn, swept into a commit and pushed. So the guard is not
"we wrote an ignore rule" but "git itself confirms this path is ignored".
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ductor_bot.handoff.guard import assert_ignored, ensure_protected, is_git_repo


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_plain_directory_is_not_a_repo(tmp_path: Path) -> None:
    """Nothing to protect means nothing to fail — not every folder is a repo."""
    assert not is_git_repo(tmp_path)

    assert ensure_protected(tmp_path).ok


def test_repo_gets_an_exclude_entry(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "proj")

    assert ensure_protected(repo).ok

    text = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "handoffs/" in text


def test_entry_is_not_duplicated(tmp_path: Path) -> None:
    """Called on every write, so it must be idempotent."""
    repo = _repo(tmp_path / "proj")

    ensure_protected(repo)
    ensure_protected(repo)
    ensure_protected(repo)

    text = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert text.count("handoffs/") == 1


def test_existing_exclude_content_is_preserved(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "proj")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("*.log\n", encoding="utf-8")

    ensure_protected(repo)

    text = exclude.read_text(encoding="utf-8")
    assert "*.log" in text
    assert "handoffs/" in text


def test_git_agrees_the_path_is_ignored(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "proj")
    ensure_protected(repo)
    target = repo / "handoffs" / "c1-t2.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    assert assert_ignored(target)


def test_unprotected_path_is_reported_as_not_ignored(tmp_path: Path) -> None:
    """The assertion has to be able to fail, or it proves nothing."""
    repo = _repo(tmp_path / "proj")
    loose = repo / "notes.md"
    loose.write_text("x", encoding="utf-8")

    assert not assert_ignored(loose)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode bits, so the failure cannot be staged")
def test_unwritable_exclude_fails_loudly(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "proj")
    info = repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    exclude = info / "exclude"
    exclude.write_text("", encoding="utf-8")
    exclude.chmod(0o400)

    try:
        result = ensure_protected(repo)

        assert not result.ok
        assert result.reason == "exclude_unwritable"
        assert "exclude" in result.detail
    finally:
        exclude.chmod(0o600)
