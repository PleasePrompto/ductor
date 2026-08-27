"""`/files` replaces `/showfiles`, which keeps working unlisted.

Renaming a command people already type should not break their muscle memory,
so the old name stays registered while only the new one appears in the menu.
"""

from __future__ import annotations

from ductor_bot.commands import BOT_COMMANDS
from ductor_bot.messenger.commands import classify_command
from ductor_bot.messenger.telegram.middleware import QUICK_COMMANDS


def test_the_menu_offers_files() -> None:
    names = [name for name, _desc in BOT_COMMANDS]
    assert "files" in names


def test_the_menu_does_not_offer_the_old_name() -> None:
    """The alias works but should not clutter the picker with a duplicate."""
    names = [name for name, _desc in BOT_COMMANDS]
    assert "showfiles" not in names


def test_both_names_are_classified() -> None:
    """An unclassified command is treated as unknown and ignored."""
    assert classify_command("files") != "unknown"
    assert classify_command("showfiles") != "unknown"


def test_both_names_bypass_the_chat_lock() -> None:
    """Browsing is read-only; queueing it behind a running agent is wrong."""
    assert "/files" in QUICK_COMMANDS
    assert "/showfiles" in QUICK_COMMANDS


def test_the_handler_is_registered_under_both_names() -> None:
    """Reads the real registration rather than trusting the wiring."""
    import inspect

    from ductor_bot.messenger.telegram.app import TelegramBot

    source = inspect.getsource(TelegramBot._register_handlers)
    assert 'Command("files", "showfiles"' in source, "the alias must stay registered"
    assert "self._on_files" in source
