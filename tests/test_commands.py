"""Tests for command definitions."""

from ductor_bot.commands import BOT_COMMANDS


def test_commands_is_list_of_tuples() -> None:
    assert isinstance(BOT_COMMANDS, list)
    for item in BOT_COMMANDS:
        assert isinstance(item, tuple)
        assert len(item) == 2
        assert isinstance(item[0], str)
        assert isinstance(item[1], str)


def test_expected_commands_present() -> None:
    names = {cmd for cmd, _ in BOT_COMMANDS}
    expected = {"new", "stop", "status", "model", "memory", "cron", "restart", "diagnose"}
    assert expected.issubset(names)


def test_no_duplicate_commands() -> None:
    names = [cmd for cmd, _ in BOT_COMMANDS]
    assert len(names) == len(set(names))
