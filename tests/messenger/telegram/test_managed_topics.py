"""Bootstrap of the Consult topic and the pinned notices.

The property that matters: running it twice must not produce two topics.
Telegram cannot list forum topics, so a mistake here is not self-correcting —
every deploy would leave another Consult behind.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from ductor_bot.i18n import init
from ductor_bot.messenger.telegram.managed_topics import (
    CONSULT,
    CONSULT_RULE,
    GENERAL,
    ManagedTopicStore,
    TopicRecord,
    ensure_consult_workspace,
    ensure_managed_topics,
)

CHAT = -100123


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SimpleNamespace(), message=message)


class FakeBot:
    """Records calls, and can be told the notice no longer exists."""

    def __init__(
        self,
        *,
        notice_alive: bool = True,
        not_modified: bool = False,
        start_id: int = 100,
    ) -> None:
        self.notice_alive = notice_alive
        self.not_modified = not_modified
        self.created: list[str] = []
        self.sent: list[tuple[int | None, str]] = []
        self.pinned: list[int] = []
        self.edited: list[int] = []
        # Telegram never reuses ids; a fake that did would hide real mix-ups.
        self._next_id = start_id

    async def create_forum_topic(self, chat_id: int, name: str):
        self.created.append(name)
        self._next_id += 1
        return SimpleNamespace(message_thread_id=self._next_id)

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self._next_id += 1
        self.sent.append((kwargs.get("message_thread_id"), text))
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, chat_id: int, message_id: int, **kwargs):
        self.edited.append(message_id)
        if self.not_modified:
            raise _bad_request("Bad Request: message is not modified")
        if not self.notice_alive:
            raise _bad_request("Bad Request: message to edit not found")

    async def pin_chat_message(self, chat_id: int, message_id: int, **kwargs):
        self.pinned.append(message_id)


@pytest.fixture(autouse=True)
def _english() -> None:
    init("en")


@pytest.fixture
def store(tmp_path: Path) -> ManagedTopicStore:
    return ManagedTopicStore(tmp_path / "managed_topics.json")


async def test_first_run_creates_the_topic_and_posts_both_notices(
    store: ManagedTopicStore, tmp_path: Path
) -> None:
    bot = FakeBot()
    await ensure_managed_topics(bot, CHAT, store, tmp_path / "Consult")

    assert bot.created == ["Consult"]
    assert len(bot.sent) == 2, "one notice in Consult, one in General"
    assert store.get(CHAT, CONSULT).topic_id is not None
    assert store.get(CHAT, GENERAL).notice_message_id is not None


async def test_only_the_general_notice_is_pinned(
    store: ManagedTopicStore, tmp_path: Path
) -> None:
    """pinChatMessage takes no message_thread_id. It pins chat-wide, and a
    message inside a forum topic is accepted and then not pinned — the call
    returns True and nothing appears. Calling it there claims something the
    platform does not do."""
    bot = FakeBot()
    await ensure_managed_topics(bot, CHAT, store, tmp_path / "Consult")

    general_notice = store.get(CHAT, GENERAL).notice_message_id
    assert bot.pinned == [general_notice], bot.pinned

    consult_notice = store.get(CHAT, CONSULT).notice_message_id
    assert consult_notice not in bot.pinned


async def test_the_consult_notice_is_the_first_message_in_its_topic(
    store: ManagedTopicStore, tmp_path: Path
) -> None:
    """Position is what makes it visible, since it cannot be pinned."""
    bot = FakeBot()
    await ensure_managed_topics(bot, CHAT, store, tmp_path / "Consult")

    topic_id = store.get(CHAT, CONSULT).topic_id
    first_in_topic = next(thread for thread, _text in bot.sent if thread is not None)
    assert first_in_topic == topic_id


async def test_second_run_creates_nothing(store: ManagedTopicStore, tmp_path: Path) -> None:
    """The whole point: Telegram cannot list topics, so this cannot self-correct."""
    consult = tmp_path / "Consult"
    await ensure_managed_topics(FakeBot(), CHAT, store, consult)
    first = store.get(CHAT, CONSULT)

    bot = FakeBot()
    await ensure_managed_topics(bot, CHAT, store, consult)

    assert bot.created == []
    assert bot.sent == []
    assert store.get(CHAT, CONSULT) == first


async def test_an_unchanged_notice_counts_as_alive(
    store: ManagedTopicStore, tmp_path: Path
) -> None:
    """Telegram errors on an edit that changes nothing; that is a success."""
    consult = tmp_path / "Consult"
    await ensure_managed_topics(FakeBot(), CHAT, store, consult)

    bot = FakeBot(not_modified=True)
    await ensure_managed_topics(bot, CHAT, store, consult)

    assert bot.created == []
    assert len(bot.pinned) == 1, "General is re-pinned; a topic notice cannot be"


async def test_a_deleted_notice_is_recreated(store: ManagedTopicStore, tmp_path: Path) -> None:
    consult = tmp_path / "Consult"
    await ensure_managed_topics(FakeBot(), CHAT, store, consult)
    before = store.get(CHAT, CONSULT).notice_message_id

    bot = FakeBot(notice_alive=False, start_id=500)
    await ensure_managed_topics(bot, CHAT, store, consult)

    assert bot.created == ["Consult"], "topic is gone with its notice, so it comes back"
    assert store.get(CHAT, CONSULT).notice_message_id != before


async def test_lost_permissions_do_not_raise(store: ManagedTopicStore, tmp_path: Path) -> None:
    """A group setting the bot cannot control must not make it unbootable."""

    class Forbidden(FakeBot):
        async def create_forum_topic(self, chat_id: int, name: str):
            raise _bad_request("Bad Request: not enough rights to manage topics")

    await ensure_managed_topics(Forbidden(), CHAT, store, tmp_path / "Consult")
    assert store.get(CHAT, CONSULT).topic_id is None


async def test_notices_go_to_the_right_places(store: ManagedTopicStore, tmp_path: Path) -> None:
    bot = FakeBot()
    await ensure_managed_topics(bot, CHAT, store, tmp_path / "Consult")

    threads = [thread for thread, _ in bot.sent]
    assert None in threads, "the General notice is not in a topic"
    assert any(thread is not None for thread in threads), "the Consult notice is"


def test_consult_workspace_carries_its_rule(tmp_path: Path) -> None:
    directory = ensure_consult_workspace(tmp_path / "Consult")
    rule = (directory / "CLAUDE.md").read_text(encoding="utf-8")
    assert rule == CONSULT_RULE
    # The wording is the only thing enforcing this, so check it says so.
    assert "not something to\noffer as an option" in rule
    assert "deleted daily" in rule


def test_the_rule_is_restored_after_a_wipe(tmp_path: Path) -> None:
    """The daily wipe removes it; the next start must put it back."""
    directory = ensure_consult_workspace(tmp_path / "Consult")
    (directory / "CLAUDE.md").unlink()

    ensure_consult_workspace(directory)
    assert (directory / "CLAUDE.md").is_file()


def test_the_rule_is_repaired_if_edited(tmp_path: Path) -> None:
    directory = ensure_consult_workspace(tmp_path / "Consult")
    (directory / "CLAUDE.md").write_text("do whatever you like", encoding="utf-8")

    ensure_consult_workspace(directory)
    assert (directory / "CLAUDE.md").read_text(encoding="utf-8") == CONSULT_RULE


def test_store_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "managed_topics.json"
    ManagedTopicStore(path).set(CHAT, CONSULT, TopicRecord(topic_id=7, notice_message_id=8))

    record = ManagedTopicStore(path).get(CHAT, CONSULT)
    assert (record.topic_id, record.notice_message_id) == (7, 8)


def test_a_damaged_store_does_not_prevent_startup(tmp_path: Path) -> None:
    path = tmp_path / "managed_topics.json"
    path.write_text("{ not json")
    assert ManagedTopicStore(path).get(CHAT, CONSULT).topic_id is None
