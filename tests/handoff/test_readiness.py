"""Refusing to start a turn the workspace cannot host.

The alternative — writing somewhere else and carrying on — works today and
leaves a second directory filling with files nobody meant to write there. These
tests pin the refusals, and pin that the safe repairs happen without one.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ductor_bot.handoff.readiness import check_readiness


def _paths(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(ductor_home=tmp_path / ".ductor")


def test_a_plain_writable_folder_is_ready(tmp_path: Path) -> None:
    folder = tmp_path / "proj"
    folder.mkdir()

    assert check_readiness(folder, _paths(tmp_path)).ok


def test_the_handoffs_directory_is_created_as_a_safe_repair(tmp_path: Path) -> None:
    folder = tmp_path / "proj"
    folder.mkdir()

    check_readiness(folder, _paths(tmp_path))

    assert (folder / "handoffs").is_dir()


def test_the_exclude_entry_is_added_as_a_safe_repair(tmp_path: Path) -> None:
    folder = tmp_path / "proj"
    folder.mkdir()
    subprocess.run(["git", "init", "-q", str(folder)], check=True)

    assert check_readiness(folder, _paths(tmp_path)).ok

    exclude = (folder / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "handoffs/" in exclude


def test_a_missing_folder_is_not_ready(tmp_path: Path) -> None:
    result = check_readiness(tmp_path / "gone", _paths(tmp_path))

    assert not result.ok
    assert result.key == "handoff.folder_missing"
    assert "gone" in result.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode bits")
def test_an_unwritable_folder_is_not_ready(tmp_path: Path) -> None:
    folder = tmp_path / "proj"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        result = check_readiness(folder, _paths(tmp_path))

        assert not result.ok
        assert result.key == "handoff.not_writable"
    finally:
        folder.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode bits")
def test_a_repo_whose_exclude_cannot_be_written_is_not_ready(tmp_path: Path) -> None:
    folder = tmp_path / "proj"
    folder.mkdir()
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    exclude = folder / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text("", encoding="utf-8")
    exclude.chmod(0o400)
    try:
        result = check_readiness(folder, _paths(tmp_path))

        assert not result.ok
        assert result.key == "handoff.exclude_unwritable"
        assert "exclude" in result.detail
    finally:
        exclude.chmod(0o600)


def test_an_unbound_conversation_checks_ductor_home(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ductor_home.mkdir(parents=True)

    assert check_readiness(None, paths).ok


def test_refusals_name_the_path_so_they_can_be_fixed(tmp_path: Path) -> None:
    """A message saying only "something went wrong" cannot be acted on."""
    result = check_readiness(tmp_path / "nowhere", _paths(tmp_path))

    assert result.detail
    assert str(tmp_path) in result.detail
