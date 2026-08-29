"""The path screens: handing an absolute path to an agent without moving a file.

A bot cannot write to a clipboard, so the whole feature is "render the path in a
code span and let Telegram's tap-to-copy do the rest". What is worth pinning is
that the path shown is the real absolute one — a display label like `IT/Images`
pasted into a prompt would send an agent to a directory that does not exist.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ductor_bot.i18n import init
from ductor_bot.messenger.telegram import file_browser as fb


@pytest.fixture(autouse=True)
def _english() -> None:
    init("en")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "IT"
    (root / "SalamData" / "Images").mkdir(parents=True)
    (root / "SalamData" / "Images" / "reception.jpeg").write_bytes(b"jpeg")
    (root / "SalamData" / "notes.txt").write_text("hi", encoding="utf-8")
    return root


def _roots(root: Path) -> dict[str, str]:
    return {"IT": str(root)}


def _paths(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace=tmp_path / "workspace", ductor_home=tmp_path / ".ductor")


def test_folder_screen_shows_the_absolute_path(tree: Path, tmp_path: Path) -> None:
    target = tree / "SalamData" / "Images"

    action = fb._path_action(_paths(tmp_path), _roots(tree), target)

    assert f"`{target}`" in action.text
    assert str(target).startswith("/")


def test_each_file_gets_its_own_button(tree: Path, tmp_path: Path) -> None:
    target = tree / "SalamData" / "Images"

    action = fb._path_action(_paths(tmp_path), _roots(tree), target)
    labels = [b.text for row in action.keyboard.inline_keyboard for b in row]

    assert "📄 reception.jpeg" in labels


def test_file_screen_shows_that_file_path(tree: Path, tmp_path: Path) -> None:
    target = tree / "SalamData" / "Images" / "reception.jpeg"

    action = fb._path_file_action(_paths(tmp_path), _roots(tree), target)

    assert f"`{target}`" in action.text
    # The folder's path must not be what gets copied instead.
    assert f"`{target.parent}`\n" not in action.text


def test_a_file_token_on_the_folder_screen_still_shows_the_file(
    tree: Path, tmp_path: Path
) -> None:
    """The button is built for directories, but a token can outlive its target."""
    target = tree / "SalamData" / "notes.txt"

    action = fb._path_action(_paths(tmp_path), _roots(tree), target)

    assert f"`{target}`" in action.text


def test_a_vanished_file_falls_back_rather_than_erroring(tree: Path, tmp_path: Path) -> None:
    target = tree / "SalamData" / "deleted.txt"

    action = fb._path_file_action(_paths(tmp_path), _roots(tree), target)

    assert f"`{target}`" not in action.text


def test_path_callbacks_are_recognised() -> None:
    assert fb.is_file_browser_callback(f"{fb.SF_PATH_PREFIX}abc")
    assert fb.is_file_browser_callback(f"{fb.SF_PATH_FILE_PREFIX}abc")


def test_path_prefixes_do_not_collide_with_the_others() -> None:
    """A prefix that is also another prefix's start would route to the wrong screen."""
    everything = [
        v
        for k, v in vars(fb).items()
        if k.startswith("SF_") and k.endswith(("PREFIX",)) and isinstance(v, str)
    ]
    # Without this the loop below would pass by finding nothing to compare against.
    assert len(everything) > 20, everything
    for new in (fb.SF_PATH_PREFIX, fb.SF_PATH_FILE_PREFIX):
        others = [p for p in everything if p != new]
        assert not any(p.startswith(new) or new.startswith(p) for p in others), new
