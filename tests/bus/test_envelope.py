"""Tests for the Envelope dataclass and related enums."""

from __future__ import annotations

from ductor_bot.bus.envelope import DeliveryMode, Envelope, LockMode, Origin


def test_origin_values() -> None:
    assert Origin.BACKGROUND.value == "background"
    assert Origin.CRON.value == "cron"
    assert Origin.WEBHOOK_WAKE.value == "webhook_wake"
    assert Origin.WEBHOOK_CRON.value == "webhook_cron"
    assert Origin.HEARTBEAT.value == "heartbeat"
    assert Origin.INTERAGENT.value == "interagent"
    assert Origin.USER.value == "user"
    assert Origin.API.value == "api"


def test_delivery_mode_values() -> None:
    assert DeliveryMode.UNICAST.value == "unicast"
    assert DeliveryMode.BROADCAST.value == "broadcast"


def test_lock_mode_values() -> None:
    assert LockMode.REQUIRED.value == "required"
    assert LockMode.NONE.value == "none"


def test_envelope_defaults() -> None:
    env = Envelope(origin=Origin.CRON, chat_id=100)
    assert env.origin == Origin.CRON
    assert env.chat_id == 100
    assert env.topic_id is None
    assert env.prompt == ""
    assert env.result_text == ""
    assert env.status == ""
    assert env.is_error is False
    assert env.delivery == DeliveryMode.UNICAST
    assert env.lock_mode == LockMode.NONE
    assert env.needs_injection is False
    assert env.metadata == {}
    assert env.reply_to_message_id is None
    assert env.thread_id is None
    assert env.envelope_id == ""
    assert env.elapsed_seconds == 0.0
    assert env.provider == ""
    assert env.model == ""
    assert env.session_name == ""
    assert env.session_id == ""


def test_envelope_lock_key_without_topic() -> None:
    env = Envelope(origin=Origin.HEARTBEAT, chat_id=42)
    assert env.lock_key == (42, None)


def test_envelope_lock_key_with_topic() -> None:
    env = Envelope(origin=Origin.INTERAGENT, chat_id=42, topic_id=7)
    assert env.lock_key == (42, 7)


def test_envelope_created_at_is_set() -> None:
    env = Envelope(origin=Origin.BACKGROUND, chat_id=1)
    assert env.created_at > 0


def test_envelope_metadata_independent() -> None:
    """Each envelope gets its own metadata dict."""
    a = Envelope(origin=Origin.CRON, chat_id=1)
    b = Envelope(origin=Origin.CRON, chat_id=2)
    a.metadata["key"] = "value"
    assert "key" not in b.metadata
