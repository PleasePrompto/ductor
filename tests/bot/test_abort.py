"""Tests for abort trigger detection."""

from __future__ import annotations

import pytest


class TestIsAbortTrigger:
    """Test bare-word abort detection."""

    @pytest.mark.parametrize(
        "word",
        ["stop", "abort", "cancel", "halt", "wait", "quit", "exit", "interrupt"],
    )
    def test_english_abort_words(self, word: str) -> None:
        from ductor_bot.bot.abort import is_abort_trigger

        assert is_abort_trigger(word) is True

    @pytest.mark.parametrize("word", ["stopp", "warte", "abbruch", "abbrechen"])
    def test_german_abort_words(self, word: str) -> None:
        from ductor_bot.bot.abort import is_abort_trigger

        assert is_abort_trigger(word) is True

    def test_case_insensitive(self) -> None:
        from ductor_bot.bot.abort import is_abort_trigger

        assert is_abort_trigger("STOP") is True
        assert is_abort_trigger("Cancel") is True

    def test_whitespace_stripped(self) -> None:
        from ductor_bot.bot.abort import is_abort_trigger

        assert is_abort_trigger("  stop  ") is True

    def test_multi_word_not_trigger(self) -> None:
        from ductor_bot.bot.abort import is_abort_trigger

        assert is_abort_trigger("please stop") is False

    def test_non_abort_word(self) -> None:
        from ductor_bot.bot.abort import is_abort_trigger

        assert is_abort_trigger("hello") is False

    def test_empty_string(self) -> None:
        from ductor_bot.bot.abort import is_abort_trigger

        assert is_abort_trigger("") is False


class TestIsAbortMessage:
    """Test /stop command + bare-word detection."""

    def test_stop_command(self) -> None:
        from ductor_bot.bot.abort import is_abort_message

        assert is_abort_message("/stop") is True

    def test_stop_command_case_insensitive(self) -> None:
        from ductor_bot.bot.abort import is_abort_message

        assert is_abort_message("/STOP") is True

    def test_stop_command_with_whitespace(self) -> None:
        from ductor_bot.bot.abort import is_abort_message

        assert is_abort_message("  /stop  ") is True

    def test_bare_word_abort(self) -> None:
        from ductor_bot.bot.abort import is_abort_message

        assert is_abort_message("abort") is True

    def test_regular_message_not_abort(self) -> None:
        from ductor_bot.bot.abort import is_abort_message

        assert is_abort_message("tell me about dogs") is False

    def test_other_command_not_abort(self) -> None:
        from ductor_bot.bot.abort import is_abort_message

        assert is_abort_message("/status") is False
