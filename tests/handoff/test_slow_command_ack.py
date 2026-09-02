"""Acknowledging commands that think before they answer.

/compact and /clear consolidate the handoff first, which is a full model turn.
Without a word on screen the user assumes the tap missed and presses again —
reported from real use on the first day.
"""

from __future__ import annotations

from ductor_bot.i18n import init, t
from ductor_bot.messenger.telegram.app import _SLOW_COMMANDS
from ductor_bot.messenger.telegram.menu import MENU_ITEMS


def test_the_slow_commands_are_the_ones_that_run_a_model_turn() -> None:
    assert set(_SLOW_COMMANDS) == {"/compact", "/clear"}


def test_every_slow_command_is_reachable_from_the_menu() -> None:
    """The button is where the impatient double-press happens."""
    commands = {item.command for item in MENU_ITEMS}

    for command in _SLOW_COMMANDS:
        assert command in commands, f"{command} is not a menu button"


def test_each_acknowledgement_resolves_to_real_text() -> None:
    """A missing key renders as the key itself, which reads like a glitch."""
    init("en")

    for command, key in _SLOW_COMMANDS.items():
        rendered = t(key)
        assert rendered != key, f"{command} has no string for {key}"
        assert len(rendered) > 20, f"{command} acknowledgement is too terse to reassure"


def test_the_acknowledgement_says_it_will_take_a_moment() -> None:
    """The point is to set the expectation, not just to blink."""
    init("en")

    for key in _SLOW_COMMANDS.values():
        assert "moment" in t(key).lower()
