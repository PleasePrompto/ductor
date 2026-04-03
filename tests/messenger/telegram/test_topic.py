"""Tests for forum topic utilities."""

from __future__ import annotations

from unittest.mock import MagicMock

from aiogram.types import ForumTopicCreated, ForumTopicEdited, Message

from ductor_bot.messenger.telegram.topic import (
    TopicNameCache,
    get_thread_id,
    get_topic_name_from_message,
)
from ductor_bot.session.manager import SessionData


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


class TestGetTopicNameFromMessage:
    """Test get_topic_name_from_message helper."""

    def test_extracts_from_forum_topic_created(self) -> None:
        created = MagicMock(spec=ForumTopicCreated)
        created.name = "test topic"
        msg = MagicMock(spec=Message)
        msg.forum_topic_created = created
        msg.forum_topic_edited = None
        assert get_topic_name_from_message(msg) == "test topic"

    def test_extracts_from_forum_topic_edited(self) -> None:
        edited = MagicMock(spec=ForumTopicEdited)
        edited.name = "renamed topic"
        msg = MagicMock(spec=Message)
        msg.forum_topic_created = None
        msg.forum_topic_edited = edited
        assert get_topic_name_from_message(msg) == "renamed topic"

    def test_returns_none_when_no_service_message(self) -> None:
        msg = MagicMock(spec=Message)
        msg.forum_topic_created = None
        msg.forum_topic_edited = None
        assert get_topic_name_from_message(msg) is None

    def test_returns_none_when_edited_has_no_name(self) -> None:
        edited = MagicMock(spec=ForumTopicEdited)
        edited.name = None
        msg = MagicMock(spec=Message)
        msg.forum_topic_created = None
        msg.forum_topic_edited = edited
        assert get_topic_name_from_message(msg) is None


class TestTopicNameCache:
    """Test TopicNameCache set/get/resolve/seed."""

    def test_set_and_get(self) -> None:
        cache = TopicNameCache()
        cache.set(-100, 42, "test 1")
        assert cache.get(-100, 42) == "test 1"

    def test_get_returns_none_for_unknown(self) -> None:
        cache = TopicNameCache()
        assert cache.get(-100, 99) is None

    def test_resolve_returns_name_when_cached(self) -> None:
        cache = TopicNameCache()
        cache.set("-100", "42", "test 1")
        assert cache.resolve("-100", "42") == "test 1"

    def test_resolve_falls_back_to_topic_number(self) -> None:
        cache = TopicNameCache()
        assert cache.resolve("-100", "42") == "Topic #42"

    def test_set_overwrites_existing(self) -> None:
        cache = TopicNameCache()
        cache.set("-100", "42", "old name")
        cache.set("-100", "42", "new name")
        assert cache.get("-100", "42") == "new name"

    def test_seed_from_sessions(self) -> None:
        cache = TopicNameCache()
        sessions = [
            SessionData(chat_id="-100", topic_id="1", topic_name="Alpha"),
            SessionData(chat_id="-100", topic_id="2", topic_name="Beta"),
            SessionData(chat_id="-200", topic_id=None),  # no topic
            SessionData(chat_id="-100", topic_id="3"),  # no name
        ]
        count = cache.seed_from_sessions(sessions)
        assert count == 2
        assert cache.get("-100", "1") == "Alpha"
        assert cache.get("-100", "2") == "Beta"
        assert cache.get("-100", "3") is None

    def test_seed_does_not_overwrite_existing(self) -> None:
        cache = TopicNameCache()
        cache.set("-100", "1", "Manual")
        sessions = [SessionData(chat_id="-100", topic_id="1", topic_name="From Seed")]
        cache.seed_from_sessions(sessions)
        # seed_from_sessions does overwrite — this is intentional for startup
        assert cache.get("-100", "1") == "From Seed"

    def test_find_by_name(self) -> None:
        cache = TopicNameCache()
        cache.set("-100", "42", "test 1")
        cache.set("-100", "99", "test 2")
        assert cache.find_by_name("-100", "test 1") == "42"
        assert cache.find_by_name("-100", "test 2") == "99"

    def test_find_by_name_case_insensitive(self) -> None:
        cache = TopicNameCache()
        cache.set("-100", "42", "Test Topic")
        assert cache.find_by_name("-100", "test topic") == "42"
        assert cache.find_by_name("-100", "TEST TOPIC") == "42"

    def test_find_by_name_returns_none_for_unknown(self) -> None:
        cache = TopicNameCache()
        cache.set("-100", "42", "test 1")
        assert cache.find_by_name("-100", "nonexistent") is None

    def test_find_by_name_scoped_to_chat(self) -> None:
        cache = TopicNameCache()
        cache.set("-100", "42", "shared name")
        cache.set("-200", "99", "shared name")
        assert cache.find_by_name("-100", "shared name") == "42"
        assert cache.find_by_name("-200", "shared name") == "99"
