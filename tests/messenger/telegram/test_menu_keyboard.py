"""The persistent command panel.

Telegram supplies the collapse/expand toggle beside the input box once a
keyboard exists; the bot only decides whether there is one. The interesting
part is that a reply keyboard sends whatever is written on the button, so the
labels have to be commands the bot will actually receive.
"""

from __future__ import annotations

from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove

from ductor_bot.commands import BOT_COMMANDS
from ductor_bot.messenger.commands import classify_command
from ductor_bot.messenger.telegram.menu_keyboard import (
    MENU_ROWS,
    MenuState,
    build_menu_keyboard,
    remove_menu_keyboard,
)
from ductor_bot.messenger.telegram.middleware import QUICK_COMMANDS

BOT = "Phoenix_hq_bot"


def _labels(markup: ReplyKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


def test_every_button_is_a_real_command() -> None:
    """A reply keyboard sends its own label; a wrong one is a dead button."""
    offered = {name for name, _desc in BOT_COMMANDS}
    for row in MENU_ROWS:
        for name in row:
            assert name in offered, f"/{name} is not a registered command"


def test_buttons_are_plain_commands_by_default() -> None:
    assert _labels(build_menu_keyboard(BOT, mention=False))[:2] == ["/files", "/folder"]


def test_mention_mode_addresses_the_bot() -> None:
    """With group_mention_only, a bare /files is ignored — the panel would
    look fine and do nothing."""
    labels = _labels(build_menu_keyboard(BOT, mention=True))
    assert all(label.endswith(f"@{BOT}") for label in labels)


def test_mention_mode_without_a_username_stays_usable() -> None:
    """Before get_me resolves there is no username to append."""
    labels = _labels(build_menu_keyboard(None, mention=True))
    assert labels[0] == "/files"


def test_the_panel_resizes_and_persists() -> None:
    markup = build_menu_keyboard(BOT, mention=False)
    assert markup.resize_keyboard is True, "a full-height panel hides the conversation"
    assert markup.is_persistent is True


def test_the_panel_is_small_enough_to_be_useful() -> None:
    """A keyboard that fills the screen is worse than the menu it replaces."""
    assert len(MENU_ROWS) <= 4
    assert all(len(row) <= 2 for row in MENU_ROWS)


def test_removing_it_restores_the_plain_input() -> None:
    assert isinstance(remove_menu_keyboard(), ReplyKeyboardRemove)


def test_toggle_alternates_per_chat() -> None:
    state = MenuState()
    assert state.is_shown(-100) is False
    assert state.toggle(-100) is True
    assert state.is_shown(-100) is True
    assert state.toggle(-100) is False
    assert state.is_shown(-100) is False


def test_chats_do_not_share_panel_state() -> None:
    state = MenuState()
    state.toggle(-100)
    assert state.is_shown(-200) is False


def test_menu_is_registered_and_quick() -> None:
    assert "menu" in {name for name, _desc in BOT_COMMANDS}
    assert classify_command("menu") != "unknown"
    assert "/menu" in QUICK_COMMANDS, "showing a panel must not queue behind the agent"
