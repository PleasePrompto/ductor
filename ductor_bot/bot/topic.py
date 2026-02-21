"""Forum topic support utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram.types import Message


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
