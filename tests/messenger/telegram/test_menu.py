"""The inline menu and the one-button panel that opens it.

The design property worth protecting: exactly one command is ever sent as text.
Everything else is a callback, so nothing the menu does can be mistaken for a
message to the agent, and nothing has to be intercepted to keep it out.
"""

from __future__ import annotations

import pytest
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove

from ductor_bot.commands import BOT_COMMANDS
from ductor_bot.i18n import init
from ductor_bot.messenger.telegram.menu import (
    MENU_ITEMS,
    MNU_CLOSE,
    MNU_PREFIX,
    build_menu,
    build_toggle_panel,
    is_menu_callback,
    parse_callback,
    remove_toggle_panel,
    state_subtitle,
)

LOCALES = ("de", "en", "es", "fr", "id", "nl", "pt", "ru")


@pytest.fixture(autouse=True)
def _english() -> None:
    init("en")


def _buttons(markup) -> list:
    return [b for row in markup.inline_keyboard for b in row]


def test_every_item_names_a_real_command() -> None:
    """A menu item that is not a command is a button that does nothing."""
    offered = {f"/{name}" for name, _desc in BOT_COMMANDS}
    for item in MENU_ITEMS:
        assert item.command in offered, f"{item.command} is not registered"


def test_the_panel_sends_only_one_command() -> None:
    """The whole point: one text message, never anything else."""
    panel = build_toggle_panel()
    labels = [b.text for row in panel.keyboard for b in row]
    assert labels == ["/menu"]
    assert isinstance(panel, ReplyKeyboardMarkup)
    assert panel.resize_keyboard is True


def test_menu_actions_are_callbacks_not_text() -> None:
    """Callbacks never enter the message stream, so the agent never sees them."""
    _text, kb = build_menu()
    for button in _buttons(kb):
        assert button.callback_data, f"{button.text} would have to be typed"
        assert button.callback_data.startswith(MNU_PREFIX)


def test_callback_data_fits_telegram_limit() -> None:
    _text, kb = build_menu()
    for button in _buttons(kb):
        assert len(button.callback_data.encode()) <= 64


def test_indices_round_trip() -> None:
    for index in range(len(MENU_ITEMS)):
        assert parse_callback(f"{MNU_PREFIX}{index}") == index
    assert parse_callback(MNU_CLOSE) is None
    assert parse_callback(f"{MNU_PREFIX}nonsense") is None


def test_close_is_offered() -> None:
    _text, kb = build_menu()
    assert any(b.callback_data == MNU_CLOSE for b in _buttons(kb))


def test_only_menu_callbacks_are_claimed() -> None:
    assert is_menu_callback(f"{MNU_PREFIX}0") is True
    assert is_menu_callback("sf:abc") is False
    assert is_menu_callback("prs:1") is False


def test_state_is_shown_not_used_to_hide_buttons() -> None:
    """A button that comes and goes reads as a broken screen."""
    plain = _buttons(build_menu()[1])
    bound = _buttons(build_menu(state_subtitle("wp-website", "coder", "sonnet"))[1])
    assert [b.text for b in plain] == [b.text for b in bound]

    text, _kb = build_menu(state_subtitle("wp-website", "coder", "sonnet"))
    assert "wp-website" in text
    assert "coder" in text


def test_subtitle_omits_what_is_not_set() -> None:
    assert state_subtitle("", "", "") == ""
    assert state_subtitle("EMR", "", "") == "📁 EMR"
    assert "·" in state_subtitle("EMR", "coder", "")


def test_removing_the_panel_restores_the_plain_input() -> None:
    assert isinstance(remove_toggle_panel(), ReplyKeyboardRemove)


@pytest.mark.parametrize("locale", LOCALES)
def test_labels_fit_a_phone_in_every_language(locale: str) -> None:
    """Two per row; a long label beside a short one is silently truncated."""
    init(locale)
    try:
        offenders = [
            f"{b.text!r} ({len(b.text)})"
            for row in build_menu()[1].inline_keyboard
            if len(row) > 1
            for b in row
            if len(b.text) > 22
        ]
        assert not offenders, f"{locale}: " + ", ".join(offenders)
    finally:
        init("en")
