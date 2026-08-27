"""Tests for the browser's pull/push row."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ductor_bot.files import path_tokens
from ductor_bot.i18n import init
from ductor_bot.messenger.telegram import file_browser as fb


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={"HOME": str(cwd), "PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_TERMINAL_PROMPT": "0"},
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    path_tokens.clear()
    init("en")


@pytest.fixture
def env(tmp_path: Path):
    home = tmp_path / ".ductor"
    (home / "workspace").mkdir(parents=True)

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "--bare", "-b", "main", ".", cwd=origin)

    proj = tmp_path / "proj"
    proj.mkdir()
    _git("init", "-b", "main", ".", cwd=proj)
    _git("config", "user.email", "t@t.t", cwd=proj)
    _git("config", "user.name", "t", cwd=proj)
    (proj / "README.md").write_text("hi\n")
    _git("add", "README.md", cwd=proj)
    _git("commit", "-m", "first", cwd=proj)
    _git("remote", "add", "origin", str(origin), cwd=proj)
    _git("push", "-u", "origin", "main", cwd=proj)

    paths = SimpleNamespace(ductor_home=home, workspace=home / "workspace")
    return paths, {"proj": str(proj)}, proj


def _buttons(kb) -> list:
    return [b for row in kb.inline_keyboard for b in row]


def _open(paths, roots, target: Path):
    return fb._handle(paths, roots, f"{fb.SF_PREFIX}{path_tokens.token_for(target)}")


def test_plain_directory_has_no_git_row(env) -> None:
    paths, roots, _ = env
    action = _open(paths, roots, paths.ductor_home)
    assert not any("Pull" in b.text or "Push" in b.text for b in _buttons(action.keyboard))


def test_repository_shows_both_controls(env) -> None:
    paths, roots, proj = env
    action = _open(paths, roots, proj)
    labels = [b.text for b in _buttons(action.keyboard)]
    assert any("Pull" in x for x in labels)
    assert any("Push" in x or "Nothing to push" in x for x in labels)


def test_push_is_inert_when_nothing_to_push(env) -> None:
    """ahead is exact, so this state can be shown honestly."""
    paths, roots, proj = env
    action = _open(paths, roots, proj)
    assert any("Nothing to push" in b.text for b in _buttons(action.keyboard))


def test_push_shows_a_count_once_there_are_commits(env) -> None:
    paths, roots, proj = env
    (proj / "b.txt").write_text("two\n")
    _git("add", "b.txt", cwd=proj)
    _git("commit", "-m", "second", cwd=proj)

    action = _open(paths, roots, proj)
    assert any("Push 1" in b.text for b in _buttons(action.keyboard))


def test_push_asks_before_publishing(env) -> None:
    """The tap that authorises a push should be made against a list of what it
    will publish, not a bare count."""
    paths, roots, proj = env
    (proj / "b.txt").write_text("two\n")
    _git("add", "b.txt", cwd=proj)
    _git("commit", "-m", "a distinctive subject", cwd=proj)

    action = fb._handle(paths, roots, f"{fb.SF_PUSH_PREFIX}{path_tokens.token_for(proj)}")
    assert "a distinctive subject" in action.text
    assert any("Push 1 now" in b.text for b in _buttons(action.keyboard))

    # Nothing was published by merely asking.
    state = fb.read_state(proj)
    assert state is not None
    assert state.ahead == 1


def test_confirmed_push_publishes(env) -> None:
    paths, roots, proj = env
    (proj / "b.txt").write_text("two\n")
    _git("add", "b.txt", cwd=proj)
    _git("commit", "-m", "second", cwd=proj)

    fb._handle(paths, roots, f"{fb.SF_PUSH_CONFIRM_PREFIX}{path_tokens.token_for(proj)}")
    state = fb.read_state(proj)
    assert state is not None
    assert state.ahead == 0


def test_push_on_a_clean_repo_says_so(env) -> None:
    paths, roots, proj = env
    action = fb._handle(paths, roots, f"{fb.SF_PUSH_PREFIX}{path_tokens.token_for(proj)}")
    assert "Nothing to push" in action.text


def test_repo_without_upstream_offers_no_actions(env, tmp_path: Path) -> None:
    paths, _, _ = env
    solo = tmp_path / "solo"
    solo.mkdir()
    _git("init", "-b", "main", ".", cwd=solo)
    _git("config", "user.email", "t@t.t", cwd=solo)
    _git("config", "user.name", "t", cwd=solo)
    (solo / "x.txt").write_text("x")
    _git("add", "x.txt", cwd=solo)
    _git("commit", "-m", "only", cwd=solo)

    action = _open(paths, {"solo": str(solo)}, solo)
    labels = [b.text for b in _buttons(action.keyboard)]
    assert any("No upstream" in x for x in labels)
    assert not any("Push 1" in x for x in labels)


def test_git_callbacks_stay_within_the_payload_limit(env) -> None:
    paths, roots, proj = env
    deep = proj / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    action = _open(paths, roots, deep)
    for b in _buttons(action.keyboard):
        assert len(b.callback_data.encode()) <= 64


def test_push_confirm_prefix_is_not_read_as_a_file_send() -> None:
    """sf!! begins with sf!, so prefix order in the parser matters."""
    prefix, _ = fb._parse(f"{fb.SF_PUSH_CONFIRM_PREFIX}abc")
    assert prefix == fb.SF_PUSH_CONFIRM_PREFIX
