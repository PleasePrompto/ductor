"""Tests for abort trigger detection."""

from __future__ import annotations

import pytest


class TestIsAbortTrigger:
    """Test bare-word abort detection."""

    @pytest.mark.parametrize(
        "word",
        ["stop", "abort", "cancel", "halt", "hold", "wait", "quit", "exit"],
    )
    def test_english_abort_words(self, word: str) -> None:
        from ductor_bot.bot.abort import is_abort_trigger

        assert is_abort_trigger(word) is True

    @pytest.mark.parametrize("word", ["esc", "interrupt"])
    def test_interrupt_words_not_abort(self, word: str) -> None:
        """'esc' and 'interrupt' are now handled by is_interrupt_trigger."""
        from ductor_bot.bot.abort import is_abort_trigger

        assert is_abort_trigger(word) is False

    @pytest.mark.parametrize("word", ["stopp", "warte", "abbruch", "abbrechen", "aufhören"])
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

    def test_stop_command_with_bot_mention(self) -> None:
        from ductor_bot.bot.abort import is_abort_message

        assert is_abort_message("/stop@ductor_bot") is True

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

    def test_stop_all_not_single_abort(self) -> None:
        from ductor_bot.bot.abort import is_abort_message

        # "stop all" is multi-word, so is_abort_message should NOT match
        assert is_abort_message("stop all") is False


class TestIsAbortAllTrigger:
    """Test multi-word 'stop all' detection."""

    @pytest.mark.parametrize(
        "phrase",
        ["stop all", "stopp alle", "alles stoppen", "cancel all", "abort all"],
    )
    def test_abort_all_phrases(self, phrase: str) -> None:
        from ductor_bot.bot.abort import is_abort_all_trigger

        assert is_abort_all_trigger(phrase) is True

    def test_case_insensitive(self) -> None:
        from ductor_bot.bot.abort import is_abort_all_trigger

        assert is_abort_all_trigger("STOP ALL") is True
        assert is_abort_all_trigger("Cancel All") is True

    def test_whitespace_stripped(self) -> None:
        from ductor_bot.bot.abort import is_abort_all_trigger

        assert is_abort_all_trigger("  stop all  ") is True

    def test_single_word_not_trigger(self) -> None:
        from ductor_bot.bot.abort import is_abort_all_trigger

        assert is_abort_all_trigger("stop") is False

    def test_non_abort_phrase(self) -> None:
        from ductor_bot.bot.abort import is_abort_all_trigger

        assert is_abort_all_trigger("hello world") is False


class TestIsAbortAllMessage:
    """Test /stop_all command + phrase detection."""

    def test_stop_all_command(self) -> None:
        from ductor_bot.bot.abort import is_abort_all_message

        assert is_abort_all_message("/stop_all") is True

    def test_stop_all_command_case_insensitive(self) -> None:
        from ductor_bot.bot.abort import is_abort_all_message

        assert is_abort_all_message("/STOP_ALL") is True

    def test_stop_all_command_with_bot_mention(self) -> None:
        from ductor_bot.bot.abort import is_abort_all_message

        assert is_abort_all_message("/stop_all@ductor_bot") is True

    def test_bare_phrase_abort_all(self) -> None:
        from ductor_bot.bot.abort import is_abort_all_message

        assert is_abort_all_message("stop all") is True

    def test_regular_message_not_abort_all(self) -> None:
        from ductor_bot.bot.abort import is_abort_all_message

        assert is_abort_all_message("please stop everything") is False

    def test_single_stop_not_abort_all(self) -> None:
        from ductor_bot.bot.abort import is_abort_all_message

        assert is_abort_all_message("stop") is False

    def test_other_command_not_abort_all(self) -> None:
        from ductor_bot.bot.abort import is_abort_all_message

        assert is_abort_all_message("/stop") is False


class TestIsInterruptTrigger:
    """Test bare-word interrupt detection."""

    @pytest.mark.parametrize("word", ["esc", "interrupt"])
    def test_interrupt_words(self, word: str) -> None:
        from ductor_bot.bot.abort import is_interrupt_trigger

        assert is_interrupt_trigger(word) is True

    def test_case_insensitive(self) -> None:
        from ductor_bot.bot.abort import is_interrupt_trigger

        assert is_interrupt_trigger("ESC") is True
        assert is_interrupt_trigger("Interrupt") is True

    def test_non_interrupt_word(self) -> None:
        from ductor_bot.bot.abort import is_interrupt_trigger

        assert is_interrupt_trigger("stop") is False

    def test_multi_word_not_trigger(self) -> None:
        from ductor_bot.bot.abort import is_interrupt_trigger

        assert is_interrupt_trigger("please interrupt") is False


class TestIsInterruptMessage:
    """Test /interrupt command + bare-word detection."""

    def test_interrupt_command(self) -> None:
        from ductor_bot.bot.abort import is_interrupt_message

        assert is_interrupt_message("/interrupt") is True

    def test_interrupt_command_bang(self) -> None:
        from ductor_bot.bot.abort import is_interrupt_message

        assert is_interrupt_message("!interrupt") is True

    def test_interrupt_command_with_bot_mention(self) -> None:
        from ductor_bot.bot.abort import is_interrupt_message

        assert is_interrupt_message("/interrupt@ductor_bot") is True

    def test_bare_word_interrupt(self) -> None:
        from ductor_bot.bot.abort import is_interrupt_message

        assert is_interrupt_message("esc") is True

    def test_regular_message_not_interrupt(self) -> None:
        from ductor_bot.bot.abort import is_interrupt_message

        assert is_interrupt_message("hello") is False

    def test_stop_not_interrupt(self) -> None:
        from ductor_bot.bot.abort import is_interrupt_message

        assert is_interrupt_message("/stop") is False
