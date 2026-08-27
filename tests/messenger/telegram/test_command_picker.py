"""What Telegram's "/" list offers, and what it deliberately omits.

Hiding a command from the picker is safe only while something else reaches it.
The invariant here: everything hidden is in the inline menu, and everything
hidden still works and is still documented by /help.
"""

from __future__ import annotations

import inspect

from ductor_bot.commands import (
    MENU_DUPLICATES,
    get_bot_commands,
    get_picker_commands,
)
from ductor_bot.messenger.telegram.menu import MENU_ITEMS


def _picker() -> set[str]:
    return {name for name, _desc in get_picker_commands()}


def _menu() -> set[str]:
    return {item.command.lstrip("/") for item in MENU_ITEMS}


def test_everything_hidden_is_reachable_from_the_menu() -> None:
    """The invariant. Drop an item from the menu without unhiding its command
    and the feature becomes invisible: not in the picker, not in the menu."""
    unreachable = MENU_DUPLICATES - _menu()
    assert not unreachable, f"hidden but not in the menu: {sorted(unreachable)}"


def test_hidden_commands_still_exist() -> None:
    """Hidden means absent from a list, not unregistered."""
    full = {name for name, _desc in get_bot_commands()}
    assert full >= MENU_DUPLICATES


def test_help_still_documents_everything() -> None:
    """/help renders from the full list, so nothing becomes undiscoverable."""
    from ductor_bot.messenger.telegram import app

    source = inspect.getsource(app._rebuild_commands)
    assert "get_bot_commands()" in source, "_CMD_DESC must stay complete"
    assert "get_picker_commands()" in source, "only the picker is trimmed"


def test_the_way_in_is_never_hidden() -> None:
    """/menu opens everything else; hiding it would strand the whole feature."""
    assert "menu" in _picker()


def test_the_way_out_is_never_hidden() -> None:
    """/help is in the menu but stays listed: it is how you recover when the
    menu itself is broken, which has already happened once."""
    assert "help" in _picker()
    assert "help" not in MENU_DUPLICATES


def test_urgent_commands_stay_one_keystroke_away() -> None:
    """Reaching for /stop is not a moment to open a menu."""
    for urgent in ("stop", "interrupt", "reset"):
        assert urgent in _picker(), f"/{urgent} must stay in the picker"


def test_commands_absent_from_the_menu_are_still_offered() -> None:
    """Anything the menu does not cover must remain visible somewhere."""
    full = {name for name, _desc in get_bot_commands()}
    for name in full - _menu():
        assert name in _picker(), f"/{name} is in neither the menu nor the picker"


def test_the_picker_actually_got_shorter() -> None:
    assert len(get_picker_commands()) < len(get_bot_commands())


def test_the_import_time_list_is_trimmed_too() -> None:
    """_sync_commands can run before any rebuild, and publishes whatever the
    module-level list holds. Trimming only the rebuild path left the full
    twenty-four being published on a fresh start."""
    from ductor_bot.messenger.telegram import app

    assert len(app._BOT_COMMANDS) == len(get_picker_commands())
    published = {c.command for c in app._BOT_COMMANDS}
    assert not (published & MENU_DUPLICATES), sorted(published & MENU_DUPLICATES)
