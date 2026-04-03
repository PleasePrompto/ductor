"""Tests for SessionKey transport-prefixed storage keys."""

from __future__ import annotations

import pytest

from ductor_bot.session.key import SessionKey


class TestStorageKey:
    def test_telegram_flat(self) -> None:
        key = SessionKey(transport="tg", chat_id="123")
        assert key.storage_key == "tg:123"

    def test_telegram_with_topic(self) -> None:
        key = SessionKey(transport="tg", chat_id="123", topic_id="45")
        assert key.storage_key == "tg:123:45"

    def test_matrix_flat(self) -> None:
        key = SessionKey(transport="mx", chat_id="999")
        assert key.storage_key == "mx:999"

    def test_matrix_with_topic(self) -> None:
        key = SessionKey(transport="mx", chat_id="42", topic_id="7")
        assert key.storage_key == "mx:42:7"

    def test_default_transport_is_telegram(self) -> None:
        key = SessionKey(chat_id="123")
        assert key.transport == "tg"
        assert key.storage_key == "tg:123"

    def test_negative_chat_id(self) -> None:
        key = SessionKey(transport="tg", chat_id="-100123")
        assert key.storage_key == "tg:-100123"

    def test_api_transport(self) -> None:
        key = SessionKey(transport="api", chat_id="1", topic_id="5")
        assert key.storage_key == "api:1:5"


class TestParse:
    def test_prefixed_telegram(self) -> None:
        key = SessionKey.parse("tg:123")
        assert key == SessionKey(transport="tg", chat_id="123")

    def test_prefixed_matrix(self) -> None:
        key = SessionKey.parse("mx:999")
        assert key == SessionKey(transport="mx", chat_id="999")

    def test_prefixed_with_topic(self) -> None:
        key = SessionKey.parse("tg:123:45")
        assert key == SessionKey(transport="tg", chat_id="123", topic_id="45")

    def test_legacy_flat(self) -> None:
        key = SessionKey.parse("123")
        assert key == SessionKey(transport="tg", chat_id="123")

    def test_legacy_with_topic(self) -> None:
        key = SessionKey.parse("123:45")
        assert key == SessionKey(transport="tg", chat_id="123", topic_id="45")

    def test_negative_chat_id(self) -> None:
        key = SessionKey.parse("tg:-100123")
        assert key == SessionKey(transport="tg", chat_id="-100123")

    def test_legacy_negative_chat_id(self) -> None:
        key = SessionKey.parse("-100123")
        assert key == SessionKey(transport="tg", chat_id="-100123")

    def test_legacy_negative_chat_id_with_topic(self) -> None:
        key = SessionKey.parse("-100123:45")
        assert key == SessionKey(transport="tg", chat_id="-100123", topic_id="45")

    def test_prefixed_negative_chat_id_with_topic(self) -> None:
        key = SessionKey.parse("tg:-100123:45")
        assert key == SessionKey(transport="tg", chat_id="-100123", topic_id="45")

    def test_roundtrip(self) -> None:
        cases = [
            SessionKey(transport="tg", chat_id="123"),
            SessionKey(transport="tg", chat_id="123", topic_id="45"),
            SessionKey(transport="mx", chat_id="999"),
            SessionKey(transport="mx", chat_id="-200", topic_id="7"),
            SessionKey(transport="api", chat_id="1", topic_id="5"),
            SessionKey(chat_id="0"),
        ]
        for key in cases:
            assert SessionKey.parse(key.storage_key) == key

    def test_many_parts_best_effort(self) -> None:
        key = SessionKey.parse("a:b:c:d")
        assert key.transport == "a"
        assert key.chat_id == "b:c:d"
        assert key.topic_id is None


class TestLockKey:
    def test_excludes_transport(self) -> None:
        key = SessionKey(transport="tg", chat_id="123", topic_id="45")
        assert key.lock_key == ("123", "45")

    def test_flat_lock_key(self) -> None:
        key = SessionKey(transport="tg", chat_id="123")
        assert key.lock_key == ("123", None)

    def test_same_lock_key_different_transport(self) -> None:
        tg = SessionKey(transport="tg", chat_id="123")
        mx = SessionKey(transport="mx", chat_id="123")
        assert tg.lock_key == mx.lock_key

    def test_different_lock_key_different_chat(self) -> None:
        a = SessionKey(transport="tg", chat_id="1")
        b = SessionKey(transport="tg", chat_id="2")
        assert a.lock_key != b.lock_key


class TestFactoryMethods:
    """Tests for SessionKey factory classmethods."""

    def test_telegram_flat(self) -> None:
        key = SessionKey.telegram(chat_id="123")
        assert key == SessionKey(transport="tg", chat_id="123")

    def test_telegram_with_topic(self) -> None:
        key = SessionKey.telegram(chat_id="123", topic_id="45")
        assert key == SessionKey(transport="tg", chat_id="123", topic_id="45")

    def test_telegram_negative_chat_id(self) -> None:
        key = SessionKey.telegram(chat_id="-100123", topic_id="7")
        assert key.transport == "tg"
        assert key.chat_id == "-100123"
        assert key.topic_id == "7"

    def test_matrix_flat(self) -> None:
        key = SessionKey.matrix(room_id="999")
        assert key == SessionKey(transport="mx", chat_id="999")

    def test_matrix_no_topic(self) -> None:
        key = SessionKey.matrix(room_id="42")
        assert key.topic_id is None

    def test_for_transport_generic(self) -> None:
        key = SessionKey.for_transport("api", chat_id="1", topic_id="5")
        assert key == SessionKey(transport="api", chat_id="1", topic_id="5")

    def test_for_transport_default_topic(self) -> None:
        key = SessionKey.for_transport("custom", chat_id="10")
        assert key.topic_id is None

    def test_factory_roundtrip(self) -> None:
        """Factory-created keys round-trip through storage_key/parse."""
        cases = [
            SessionKey.telegram(chat_id="123"),
            SessionKey.telegram(chat_id="123", topic_id="45"),
            SessionKey.matrix(room_id="999"),
            SessionKey.for_transport("api", chat_id="1", topic_id="5"),
        ]
        for key in cases:
            assert SessionKey.parse(key.storage_key) == key

    def test_factory_keys_are_frozen(self) -> None:
        key = SessionKey.telegram(chat_id="1")
        with pytest.raises(AttributeError):
            key.chat_id = 2  # type: ignore[misc]

    def test_factory_keys_are_hashable(self) -> None:
        keys = {
            SessionKey.telegram(chat_id="1"),
            SessionKey.telegram(chat_id="1"),
            SessionKey.matrix(room_id="1"),
        }
        assert len(keys) == 2


class TestFrozenAndEquality:
    def test_frozen(self) -> None:
        key = SessionKey(chat_id="1")
        with pytest.raises(AttributeError):
            key.chat_id = 2  # type: ignore[misc]

    def test_equality(self) -> None:
        a = SessionKey(transport="tg", chat_id="1", topic_id="2")
        b = SessionKey(transport="tg", chat_id="1", topic_id="2")
        assert a == b

    def test_inequality_transport(self) -> None:
        a = SessionKey(transport="tg", chat_id="1")
        b = SessionKey(transport="mx", chat_id="1")
        assert a != b

    def test_hashable(self) -> None:
        keys = {
            SessionKey(transport="tg", chat_id="1"),
            SessionKey(transport="tg", chat_id="1"),
            SessionKey(transport="mx", chat_id="1"),
        }
        assert len(keys) == 2
