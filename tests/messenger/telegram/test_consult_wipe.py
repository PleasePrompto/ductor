"""Consult wipe: when it fires, and what it does when it does.

This is the only scheduled job here that destroys data the user cannot get
back, so the tests care as much about when it *doesn't* run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from ductor_bot.i18n import init
from ductor_bot.messenger.telegram.consult_wipe import (
    DEFAULT_HOUR,
    SCHEDULES,
    is_due,
    schedule_label,
    wipe_consult,
)
from ductor_bot.messenger.telegram.managed_topics import (
    CONSULT,
    ManagedTopicStore,
    TopicRecord,
    consult_notice,
    ensure_managed_topics,
)

CHAT = -100123
# Aware on purpose: the observer works in the user's timezone, so a naive
# fixture would test a case the scheduler never sees.
MONDAY_4AM = datetime(2026, 8, 24, 4, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _english() -> None:
    init("en")


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def test_off_never_fires() -> None:
    assert is_due("off", DEFAULT_HOUR, MONDAY_4AM, None) is False
    assert is_due("off", DEFAULT_HOUR, MONDAY_4AM, MONDAY_4AM - timedelta(days=30)) is False


def test_daily_fires_only_in_its_hour() -> None:
    assert is_due("daily", 4, MONDAY_4AM, None) is True
    assert is_due("daily", 4, MONDAY_4AM.replace(hour=5), None) is False


def test_daily_fires_once_per_day() -> None:
    assert is_due("daily", 4, MONDAY_4AM, MONDAY_4AM - timedelta(minutes=30)) is False
    assert is_due("daily", 4, MONDAY_4AM, MONDAY_4AM - timedelta(days=1)) is True


def test_a_restart_does_not_trigger_a_wipe() -> None:
    """Never-wiped plus wrong hour must wait; losing data on restart is not ok."""
    assert is_due("daily", 4, MONDAY_4AM.replace(hour=13), None) is False


def test_hourly_fires_each_hour_but_not_twice() -> None:
    assert is_due("hourly", 4, MONDAY_4AM, MONDAY_4AM - timedelta(hours=1)) is True
    assert is_due("hourly", 4, MONDAY_4AM, MONDAY_4AM - timedelta(minutes=5)) is False


def test_six_hourly_fires_on_its_boundaries() -> None:
    for hour in (0, 6, 12, 18):
        assert is_due("6h", 4, MONDAY_4AM.replace(hour=hour), None) is True
    for hour in (1, 5, 7, 23):
        assert is_due("6h", 4, MONDAY_4AM.replace(hour=hour), None) is False


def test_weekly_fires_on_monday_only() -> None:
    assert is_due("weekly", 4, MONDAY_4AM, None) is True
    assert is_due("weekly", 4, MONDAY_4AM + timedelta(days=1), None) is False
    assert is_due("weekly", 4, MONDAY_4AM, MONDAY_4AM - timedelta(days=3)) is False
    assert is_due("weekly", 4, MONDAY_4AM, MONDAY_4AM - timedelta(days=8)) is True


def test_every_schedule_has_a_label() -> None:
    for name in SCHEDULES:
        label = schedule_label(name, DEFAULT_HOUR)
        assert label, f"{name} has no label"
        assert "{" not in label, f"{name} left a placeholder: {label!r}"


# ---------------------------------------------------------------------------
# The notice states the real schedule
# ---------------------------------------------------------------------------


def test_notice_names_the_configured_schedule() -> None:
    assert "Schedule: Every hour" in consult_notice("hourly", 4)
    assert "Schedule: Daily at 04:00" in consult_notice("daily", 4)


def test_notice_does_not_promise_a_wipe_when_switched_off() -> None:
    """The sentence people decide what to paste on must not be wrong."""
    text = consult_notice("off", 4)
    assert "switched off" in text
    assert "is deleted" not in text


# ---------------------------------------------------------------------------
# The wipe itself
# ---------------------------------------------------------------------------


class FakeBot:
    def __init__(self, *, can_delete: bool = True, start_id: int = 200) -> None:
        self.can_delete = can_delete
        self.deleted: list[int] = []
        self.created: list[str] = []
        self.sent: list[int | None] = []
        self.pinned: list[int] = []
        self._next_id = start_id

    async def delete_forum_topic(self, chat_id: int, message_thread_id: int):
        if not self.can_delete:
            raise TelegramBadRequest(
                method=SimpleNamespace(), message="Bad Request: not enough rights"
            )
        self.deleted.append(message_thread_id)

    async def create_forum_topic(self, chat_id: int, name: str):
        self.created.append(name)
        self._next_id += 1
        return SimpleNamespace(message_thread_id=self._next_id)

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self._next_id += 1
        self.sent.append(kwargs.get("message_thread_id"))
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, **kwargs):
        raise TelegramBadRequest(method=SimpleNamespace(), message="message to edit not found")

    async def pin_chat_message(self, chat_id: int, message_id: int, **kwargs):
        self.pinned.append(message_id)


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[ManagedTopicStore, Path]:
    return ManagedTopicStore(tmp_path / "state.json"), tmp_path / "Consult"


async def test_wipe_replaces_the_topic_and_clears_the_directory(prepared) -> None:
    store, consult = prepared
    await ensure_managed_topics(FakeBot(), CHAT, store, consult)
    original = store.get(CHAT, CONSULT).topic_id
    (consult / "notes.md").write_text("something said in confidence")

    bot = FakeBot(start_id=900)
    assert await wipe_consult(bot, CHAT, store, consult) is True

    assert bot.deleted == [original], "the old topic is removed, with its messages"
    assert not (consult / "notes.md").exists()
    new_id = store.get(CHAT, CONSULT).topic_id
    assert new_id is not None
    assert new_id != original, "a new id means a new session; nothing resumes"


async def test_the_rule_file_comes_back_after_a_wipe(prepared) -> None:
    store, consult = prepared
    await ensure_managed_topics(FakeBot(), CHAT, store, consult)

    await wipe_consult(FakeBot(start_id=900), CHAT, store, consult)
    assert (consult / "CLAUDE.md").is_file()


async def test_a_failed_delete_changes_nothing(prepared) -> None:
    """Recreating after a failed delete would leave two Consult topics, and
    Telegram cannot list them to find the orphan."""
    store, consult = prepared
    await ensure_managed_topics(FakeBot(), CHAT, store, consult)
    original = store.get(CHAT, CONSULT).topic_id
    (consult / "notes.md").write_text("still here")

    bot = FakeBot(can_delete=False, start_id=900)
    assert await wipe_consult(bot, CHAT, store, consult) is False

    assert bot.created == [], "no replacement topic"
    assert store.get(CHAT, CONSULT).topic_id == original
    assert (consult / "notes.md").exists(), "the files survive too"


async def test_wipe_without_a_recorded_topic_does_nothing(prepared) -> None:
    store, consult = prepared
    bot = FakeBot()
    assert await wipe_consult(bot, CHAT, store, consult) is False
    assert bot.deleted == []


def test_last_wipe_is_remembered(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = ManagedTopicStore(path)
    assert store.last_wipe(CHAT) is None

    store.set(CHAT, CONSULT, TopicRecord(topic_id=5, notice_message_id=6))
    store.mark_wiped(CHAT, 1_700_000_000)

    reloaded = ManagedTopicStore(path)
    assert reloaded.last_wipe(CHAT) == 1_700_000_000
    assert reloaded.get(CHAT, CONSULT).topic_id == 5, "topic records survive alongside"
