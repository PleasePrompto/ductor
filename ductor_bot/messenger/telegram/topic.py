"""Forum topic support utilities."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ductor_bot.session.key import SessionKey

if TYPE_CHECKING:
    from aiogram.types import Message

    from ductor_bot.session.manager import SessionData

logger = logging.getLogger(__name__)


def get_thread_id(message: Message | None) -> int | None:
    """Extract ``message_thread_id`` from a forum topic message.

    Returns the thread ID only when the message originates from a forum
    topic (``is_topic_message is True``).  Mirrors aiogram's internal
    logic in ``Message.answer()``.
    """
    if message is None:
        return None
    if message.is_topic_message:
        return message.message_thread_id
    return None


def get_session_key(message: Message) -> SessionKey:
    """Build a transport-agnostic ``SessionKey`` from a Telegram message.

    Forum topic messages get per-topic keys (``topic_id=message_thread_id``).
    Regular chats and non-topic supergroup messages get flat keys
    (``topic_id=None``).
    """
    topic_id = message.message_thread_id if message.is_topic_message else None
    if message.message_thread_id is not None:
        logger.debug(
            "Topic fields: is_topic_message=%s message_thread_id=%s -> topic_id=%s",
            message.is_topic_message,
            message.message_thread_id,
            topic_id,
        )
    return SessionKey.telegram(
        chat_id=str(message.chat.id),
        topic_id=str(topic_id) if topic_id is not None else None,
    )


def get_topic_name_from_message(message: Message) -> str | None:
    """Extract the topic name from ``forum_topic_created`` or ``forum_topic_edited``."""
    if message.forum_topic_created:
        return message.forum_topic_created.name
    if message.forum_topic_edited and message.forum_topic_edited.name:
        return message.forum_topic_edited.name
    return None


class TopicNameCache:
    """In-memory cache for forum topic names.

    Telegram Bot API has no ``getForumTopic`` — names are only available
    from service messages (``forum_topic_created`` / ``forum_topic_edited``).
    This cache collects them so logs and ``/status`` can show human-readable
    topic names.
    """

    def __init__(self) -> None:
        self._names: dict[tuple[str, str], str] = {}

    def set(self, chat_id: str, topic_id: str, name: str) -> None:
        """Store or update a topic name."""
        self._names[(chat_id, topic_id)] = name

    def get(self, chat_id: str, topic_id: str) -> str | None:
        """Look up a cached topic name (or ``None``)."""
        return self._names.get((chat_id, topic_id))

    def resolve(self, chat_id: str, topic_id: str) -> str:
        """Return the cached name or a fallback ``"Topic #N"``."""
        return self._names.get((chat_id, topic_id)) or f"Topic #{topic_id}"

    def find_by_name(self, chat_id: str, name: str) -> str | None:
        """Reverse lookup: return topic_id for *name* (case-insensitive) or ``None``."""
        lower = name.lower()
        for (cid, tid), cached_name in self._names.items():
            if cid == chat_id and cached_name.lower() == lower:
                return tid
        return None

    def seed_from_sessions(self, sessions: list[SessionData]) -> int:
        """Populate the cache from persisted sessions that have ``topic_name``.

        Returns the number of entries seeded.
        """
        count = 0
        for s in sessions:
            if s.topic_id is not None and s.topic_name:
                self._names[(s.chat_id, s.topic_id)] = s.topic_name
                count += 1
        return count
