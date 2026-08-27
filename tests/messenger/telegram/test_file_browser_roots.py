"""Tests for the multi-root file browser."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ductor_bot.files import path_tokens
from ductor_bot.i18n import init
from ductor_bot.messenger.telegram import file_browser as fb


@pytest.fixture(autouse=True)
def _clean() -> None:
    path_tokens.clear()
    init("en")


@pytest.fixture
def env(tmp_path: Path) -> tuple[SimpleNamespace, dict[str, str]]:
    home = tmp_path / ".ductor"
    (home / "workspace").mkdir(parents=True)
    proj = tmp_path / "IT" / "EMR"
    (proj / "src").mkdir(parents=True)
    (proj / "README.md").write_text("hello")
    (proj / "src" / "app.py").write_text("x = 1")
    (proj / ".git").mkdir()
    (proj / ".git" / "config").write_text("secret-ish")
    paths = SimpleNamespace(ductor_home=home, workspace=home / "workspace")
    return paths, {"EMR": str(proj)}


def _buttons(kb) -> list:
    return [b for row in kb.inline_keyboard for b in row]


def test_root_view_lists_home_and_projects(env) -> None:
    paths, roots = env
    _text, kb = fb._build_root_view(paths, roots)
    labels = [b.text for b in _buttons(kb)]
    assert "~/.ductor/" in labels
    assert "EMR/" in labels


def test_opening_a_project_lists_its_contents(env) -> None:
    paths, roots = env
    _, kb = fb._build_root_view(paths, roots)
    emr = next(b for b in _buttons(kb) if b.text == "EMR/")
    action = fb._handle(paths, roots, emr.callback_data)
    assert "README.md" in action.text
    assert "src/" in action.text


def test_hidden_directories_are_not_listed(env) -> None:
    """.git contents have no business in a chat listing."""
    paths, roots = env
    _, kb = fb._build_root_view(paths, roots)
    emr = next(b for b in _buttons(kb) if b.text == "EMR/")
    action = fb._handle(paths, roots, emr.callback_data)
    assert ".git" not in action.text


def test_tapping_a_file_requests_a_send(env) -> None:
    paths, roots = env
    target = Path(roots["EMR"]) / "README.md"
    action = fb._handle(paths, roots, f"{fb.SF_FILE_PREFIX}{path_tokens.token_for(target)}")
    assert action.send_path == target.resolve()


def test_oversized_file_is_refused_with_an_explanation(env, monkeypatch) -> None:
    paths, roots = env
    monkeypatch.setattr(fb, "_MAX_SEND_BYTES", 1)
    target = Path(roots["EMR"]) / "README.md"
    action = fb._handle(paths, roots, f"{fb.SF_FILE_PREFIX}{path_tokens.token_for(target)}")
    assert action.send_path is None
    assert "README.md" in action.text


def test_paths_outside_every_root_are_refused(env, tmp_path: Path) -> None:
    """Containment still holds now that there is more than one root."""
    paths, roots = env
    outside = tmp_path / "outside"
    outside.mkdir()
    action = fb._handle(paths, roots, f"{fb.SF_PREFIX}{path_tokens.token_for(outside)}")
    # Falls back to the root view rather than listing it.
    assert "EMR/" in action.text or action.keyboard is not None
    assert "outside" not in action.text


def test_unknown_token_falls_back_to_roots(env) -> None:
    """Buttons outlive restarts; an evicted token must not error."""
    paths, roots = env
    action = fb._handle(paths, roots, f"{fb.SF_PREFIX}0123456789")
    assert action.keyboard is not None
    assert "EMR/" in [b.text for b in _buttons(action.keyboard)]


def test_every_callback_payload_fits_the_limit(env) -> None:
    paths, roots = env
    _, kb = fb._build_root_view(paths, roots)
    emr = next(b for b in _buttons(kb) if b.text == "EMR/")
    action = fb._handle(paths, roots, emr.callback_data)
    for b in _buttons(action.keyboard):
        assert len(b.callback_data.encode()) <= 64


def test_zip_excludes_hidden_entries(env) -> None:
    """The archive should match what the listing showed."""
    import zipfile

    _paths, roots = env
    archive, err = fb.build_zip(Path(roots["EMR"]))
    assert archive is not None, err
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert "README.md" in names
    assert not any(n.startswith(".git") for n in names)


def test_zip_refuses_oversized_directories(env, monkeypatch) -> None:
    _paths, roots = env
    monkeypatch.setattr(fb, "_MAX_SEND_BYTES", 1)
    archive, err = fb.build_zip(Path(roots["EMR"]))
    assert archive is None
    assert err == "file_browser.zip_too_large"
