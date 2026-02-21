"""Tests for forum topic utilities."""

from __future__ import annotations

from unittest.mock import MagicMock

from aiogram.types import Message

from ductor_bot.bot.topic import get_thread_id


class TestGetThreadId:
    """Test get_thread_id utility."""

    def test_returns_none_for_none_message(self) -> None:
        assert get_thread_id(None) is None

    def test_returns_none_when_not_topic_message(self) -> None:
        msg = MagicMock(spec=Message)
        msg.is_topic_message = None
        msg.message_thread_id = 42
        assert get_thread_id(msg) is None

    def test_returns_none_when_is_topic_false(self) -> None:
        msg = MagicMock(spec=Message)
        msg.is_topic_message = False
        msg.message_thread_id = 42
        assert get_thread_id(msg) is None

    def test_returns_thread_id_when_topic_message(self) -> None:
        msg = MagicMock(spec=Message)
        msg.is_topic_message = True
        msg.message_thread_id = 123
        assert get_thread_id(msg) == 123

    def test_returns_none_when_topic_true_but_thread_id_none(self) -> None:
        msg = MagicMock(spec=Message)
        msg.is_topic_message = True
        msg.message_thread_id = None
        assert get_thread_id(msg) is None

    def test_general_topic_thread_id_one(self) -> None:
        """The 'General' topic has message_thread_id=1."""
        msg = MagicMock(spec=Message)
        msg.is_topic_message = True
        msg.message_thread_id = 1
        assert get_thread_id(msg) == 1
