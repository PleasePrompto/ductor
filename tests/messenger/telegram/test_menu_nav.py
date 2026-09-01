"""Every screen below the menu can be left, and can go back to the menu.

The property worth pinning is that this holds without each screen opting in.
Selectors and browser views are built in a dozen places; the row is attached on
the way out, so a screen added later inherits it and cannot be the one dead end
a user gets stuck on.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.i18n import init
from ductor_bot.messenger.telegram import file_browser as fb
from ductor_bot.messenger.telegram.callbacks import button_grid_to_markup
from ductor_bot.messenger.telegram.menu import (
    MNU_BACK,
    MNU_CLOSE,
    build_menu,
    with_nav,
)
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid

LOCALES = ("de", "en", "es", "fr", "id", "nl", "pt", "ru")


@pytest.fixture(autouse=True)
def _english() -> None:
    init("en")


def _data(markup: InlineKeyboardMarkup) -> list[str | None]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _grid() -> ButtonGrid:
    return ButtonGrid(rows=[[Button(text="coder", callback_data="prs:0")]])


def test_a_selector_screen_offers_both_ways_out() -> None:
    """Persona, model, folder, skills — all of them reach the user through here."""
    markup = button_grid_to_markup(_grid())

    assert MNU_BACK in _data(markup)
    assert MNU_CLOSE in _data(markup)


def test_the_selector_keeps_its_own_buttons() -> None:
    markup = button_grid_to_markup(_grid())

    assert "prs:0" in _data(markup)


def test_the_menu_itself_offers_no_way_back_to_itself() -> None:
    """It is the top. A back button there would be a button that does nothing."""
    _text, markup = build_menu()

    assert MNU_CLOSE in _data(markup)
    assert MNU_BACK not in _data(markup)


def test_the_row_is_added_once() -> None:
    """A screen built from another must not grow a second navigation row."""
    once = with_nav(InlineKeyboardMarkup(inline_keyboard=[]))

    twice = with_nav(once)

    assert _data(twice).count(MNU_CLOSE) == 1


def test_a_screen_with_no_buttons_still_offers_a_way_out() -> None:
    """The empty case — no roots, nothing to pick — is where being stuck hurts."""
    markup = with_nav(InlineKeyboardMarkup(inline_keyboard=[]))

    assert _data(markup) == [MNU_BACK, MNU_CLOSE]


def test_nothing_is_attached_to_a_message_with_no_keyboard() -> None:
    assert with_nav(None) is None
    assert button_grid_to_markup(None) is None


# ---------------------------------------------------------------------------
# File browser
# ---------------------------------------------------------------------------


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "IT"
    (root / "SalamData").mkdir(parents=True)
    (root / "SalamData" / "notes.txt").write_text("hi", encoding="utf-8")
    return root


def _paths(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace=tmp_path / "workspace", ductor_home=tmp_path / ".ductor")


@pytest.mark.asyncio
async def test_the_browser_opening_view_offers_both_ways_out(tree: Path, tmp_path: Path) -> None:
    _text, markup = await fb.file_browser_start(_paths(tmp_path), {"IT": str(tree)})

    assert MNU_BACK in _data(markup)
    assert MNU_CLOSE in _data(markup)


@pytest.mark.asyncio
async def test_a_directory_reached_by_tapping_offers_both_ways_out(
    tree: Path, tmp_path: Path
) -> None:
    from ductor_bot.files.path_tokens import token_for

    target = tree / "SalamData"
    action = await fb.handle_file_browser_callback(
        _paths(tmp_path), {"IT": str(tree)}, f"{fb.SF_PREFIX}{token_for(target)}"
    )

    assert MNU_BACK in _data(action.keyboard)
    assert MNU_CLOSE in _data(action.keyboard)


@pytest.mark.asyncio
async def test_the_browsers_own_back_still_means_the_parent_directory(
    tree: Path, tmp_path: Path
) -> None:
    """Two backs on one screen, so they must not be confusable or duplicated."""
    from ductor_bot.files.path_tokens import token_for

    target = tree / "SalamData"
    action = await fb.handle_file_browser_callback(
        _paths(tmp_path), {"IT": str(tree)}, f"{fb.SF_PREFIX}{token_for(target)}"
    )

    labels = {b.text for row in action.keyboard.inline_keyboard for b in row}
    assert labels >= {"◀︎ Back", "◀︎ Menu"}
    assert f"{fb.SF_PREFIX}{token_for(tree)}" in _data(action.keyboard)


def test_a_file_being_sent_has_no_screen_to_decorate(tree: Path) -> None:
    """Attaching a keyboard to a document upload would be a keyboard on nothing."""
    action = fb.BrowserAction(send_path=tree / "SalamData" / "notes.txt")

    assert action.keyboard is None


@pytest.mark.parametrize("locale", LOCALES)
def test_the_pair_fits_one_row_on_a_phone(locale: str) -> None:
    init(locale)
    try:
        row = with_nav(InlineKeyboardMarkup(inline_keyboard=[])).inline_keyboard[-1]
        assert isinstance(row[0], InlineKeyboardButton)
        for button in row:
            assert len(button.text) <= 22, f"{locale}: {button.text!r}"
    finally:
        init("en")
