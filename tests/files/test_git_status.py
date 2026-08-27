"""Tests for the repository state behind the browser's pull/push buttons.

These use real repositories in tmp_path rather than mocks: the whole point of
the module is what git actually reports, and a mocked subprocess would only
assert that I remembered my own argument strings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ductor_bot.files.git_status import (
    pending_commits,
    pull,
    push,
    read_state,
    repo_root,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={"HOME": str(cwd), "PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_TERMINAL_PROMPT": "0"},
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A local clone with an upstream, so ahead/behind are meaningful."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "--bare", "-b", "main", ".", cwd=origin)

    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", ".", cwd=work)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "a.txt").write_text("one\n")
    _git("add", "a.txt", cwd=work)
    _git("commit", "-m", "first", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)
    return work


def test_non_repository_returns_nothing(tmp_path: Path) -> None:
    assert read_state(tmp_path) is None
    assert repo_root(tmp_path) is None


def test_clean_repo_has_nothing_to_do(repo: Path) -> None:
    state = read_state(repo)
    assert state is not None
    assert state.branch == "main"
    assert state.ahead == 0
    assert state.behind == 0
    assert state.dirty == 0
    assert state.has_upstream
    assert not state.can_push


def test_local_commit_makes_push_available(repo: Path) -> None:
    (repo / "b.txt").write_text("two\n")
    _git("add", "b.txt", cwd=repo)
    _git("commit", "-m", "second", cwd=repo)

    state = read_state(repo)
    assert state is not None
    assert state.ahead == 1
    assert state.can_push
    assert any("second" in line for line in pending_commits(state))


def test_uncommitted_changes_are_counted(repo: Path) -> None:
    (repo / "a.txt").write_text("changed\n")
    state = read_state(repo)
    assert state is not None
    assert state.dirty == 1


def test_state_is_found_from_a_subdirectory(repo: Path) -> None:
    sub = repo / "nested" / "deeper"
    sub.mkdir(parents=True)
    state = read_state(sub)
    assert state is not None
    assert state.root == repo.resolve()


def test_repo_without_upstream_offers_no_actions(tmp_path: Path) -> None:
    solo = tmp_path / "solo"
    solo.mkdir()
    _git("init", "-b", "main", ".", cwd=solo)
    _git("config", "user.email", "t@t.t", cwd=solo)
    _git("config", "user.name", "t", cwd=solo)
    (solo / "a.txt").write_text("x")
    _git("add", "a.txt", cwd=solo)
    _git("commit", "-m", "only", cwd=solo)

    state = read_state(solo)
    assert state is not None
    assert not state.has_upstream
    assert not state.can_push


def test_push_publishes_and_clears_ahead(repo: Path) -> None:
    (repo / "b.txt").write_text("two\n")
    _git("add", "b.txt", cwd=repo)
    _git("commit", "-m", "second", cwd=repo)

    ok, _ = push(read_state(repo))  # type: ignore[arg-type]
    assert ok
    after = read_state(repo)
    assert after is not None
    assert after.ahead == 0


def test_pull_fast_forwards(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True)
    _git("config", "user.email", "o@o.o", cwd=other)
    _git("config", "user.name", "o", cwd=other)
    (other / "c.txt").write_text("three\n")
    _git("add", "c.txt", cwd=other)
    _git("commit", "-m", "third", cwd=other)
    _git("push", cwd=other)

    ok, _ = pull(read_state(repo))  # type: ignore[arg-type]
    assert ok
    assert (repo / "c.txt").is_file()


def test_pull_refuses_to_merge_divergent_history(repo: Path, tmp_path: Path) -> None:
    """--ff-only on purpose: a surprise merge commit made from a phone, in
    someone else's repository, is not a good outcome."""
    other = tmp_path / "other2"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True)
    _git("config", "user.email", "o@o.o", cwd=other)
    _git("config", "user.name", "o", cwd=other)
    (other / "remote.txt").write_text("remote\n")
    _git("add", "remote.txt", cwd=other)
    _git("commit", "-m", "remote work", cwd=other)
    _git("push", cwd=other)

    (repo / "local.txt").write_text("local\n")
    _git("add", "local.txt", cwd=repo)
    _git("commit", "-m", "local work", cwd=repo)

    ok, output = pull(read_state(repo))  # type: ignore[arg-type]
    assert not ok
    assert output
