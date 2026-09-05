"""Regression tests for durable interactive cron follow-up context."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from ductor_bot.bus.adapters import from_cron_result
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.cron_followup import CronFollowupStore
from ductor_bot.session.key import SessionKey


def _transport() -> AsyncMock:
    transport = AsyncMock()
    transport.transport_name = "tg"
    transport.deliver = AsyncMock()
    transport.deliver_broadcast = AsyncMock()
    return transport


async def test_interactive_unicast_survives_restart_and_is_consumed_once(tmp_path: Path) -> None:
    now = [100.0]
    path = tmp_path / "cron_followups.json"
    store = CronFollowupStore(path, clock=lambda: now[0])
    env = from_cron_result(
        "Scheduled review",
        "A pending item requires confirmation. Should it be included in the report?",
        "success",
        chat_id=42,
    )

    bus = MessageBus()
    transport = _transport()
    bus.register_transport(transport)
    bus.set_cron_followup_store(store)
    await bus.submit(env)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["entries"]["tg:42"]["result_text"].startswith("A pending item")

    restarted_store = CronFollowupStore(path, clock=lambda: now[0])
    context = await restarted_store.consume(SessionKey.telegram(42))
    assert context is not None
    assert context.title == "Scheduled review"
    assert "included in the report" in context.result_text
    assert await restarted_store.consume(SessionKey.telegram(42)) is None


async def test_followup_isolated_by_transport_and_topic(tmp_path: Path) -> None:
    now = [100.0]
    store = CronFollowupStore(tmp_path / "cron_followups.json", clock=lambda: now[0])

    for transport, topic, text in (
        ("tg", None, "Telegram question?"),
        ("tg", 7, "Telegram topic question?"),
        ("mx", None, "Matrix question?"),
    ):
        env = from_cron_result(
            "Review",
            text,
            "success",
            chat_id=42,
            topic_id=topic,
            transport=transport,
        )
        await store.record(env)

    topic = await store.consume(SessionKey.telegram(42, 7))
    assert topic is not None
    assert topic.result_text == "Telegram topic question?"
    matrix = await store.consume(SessionKey.matrix(42))
    assert matrix is not None
    assert matrix.result_text == "Matrix question?"
    telegram = await store.consume(SessionKey.telegram(42))
    assert telegram is not None
    assert telegram.result_text == "Telegram question?"


async def test_expired_followup_is_removed_without_attachment(tmp_path: Path) -> None:
    now = [100.0]
    path = tmp_path / "cron_followups.json"
    store = CronFollowupStore(path, ttl_seconds=1, clock=lambda: now[0])
    await store.record(
        from_cron_result("Review", "Please confirm this action?", "success", chat_id=42)
    )

    now[0] = 101.0
    assert await store.consume(SessionKey.telegram(42)) is None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["entries"] == {}


async def test_informative_unicast_report_does_not_create_followup(tmp_path: Path) -> None:
    store = CronFollowupStore(tmp_path / "cron_followups.json")
    env = from_cron_result("Daily report", "Nothing new to handle today.", "success", chat_id=42)

    assert env.metadata == {"title": "Daily report"}
    bus = MessageBus()
    transport = _transport()
    bus.register_transport(transport)
    bus.set_cron_followup_store(store)
    await bus.submit(env)

    assert await store.consume(SessionKey.telegram(42)) is None
    assert not (tmp_path / "cron_followups.json").exists()
