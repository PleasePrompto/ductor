"""Telegram bot middleware: auth filtering and sequential processing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
    TelegramObject,
)

from ductor_bot.bot.abort import is_abort_message
from ductor_bot.bot.dedup import DedupeCache, build_dedup_key
from ductor_bot.log_context import set_log_context

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

AbortHandler = Callable[[int, "Message"], Awaitable[bool]]
"""Async callback: (chat_id, message) -> handled?"""

QuickCommandHandler = Callable[[int, "Message"], Awaitable[bool]]
"""Async callback for read-only commands that bypass the per-chat lock."""

QUICK_COMMANDS: frozenset[str] = frozenset(
    {"/status", "/memory", "/cron", "/diagnose", "/model", "/showfiles"}
)

MQ_PREFIX = "mq:"
"""Callback data prefix for message queue cancel buttons."""


def is_quick_command(text: str) -> bool:
    """Return True if *text* is a command that can bypass the lock.

    Matches both bare commands (``/status``) and commands with arguments
    (``/model sonnet``).
    """
    cmd = text.strip().lower().split(None, 1)[0] if text.strip() else ""
    return cmd in QUICK_COMMANDS


class AuthMiddleware(BaseMiddleware):
    """Outer middleware: silently drop messages from unauthorized users."""

    def __init__(self, allowed_user_ids: set[int]) -> None:
        self._allowed = allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, (Message, CallbackQuery)):
            user = event.from_user
        else:
            return await handler(event, data)

        if not user or user.id not in self._allowed:
            return None

        return await handler(event, data)


_MAX_LOCKS = 1000


@dataclass(slots=True)
class _QueueEntry:
    """A message waiting behind the per-chat lock."""

    entry_id: int
    chat_id: int
    message_id: int
    text_preview: str
    cancelled: bool = False
    indicator_msg_id: int | None = field(default=None, repr=False)


class SequentialMiddleware(BaseMiddleware):
    """Outer middleware: dedup + per-chat lock ensures sequential processing.

    Tracks pending messages per chat so they can be individually cancelled
    (via inline keyboard) or bulk-discarded on ``/stop``.
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._dedup = DedupeCache()
        self._abort_handler: AbortHandler | None = None
        self._quick_command_handler: QuickCommandHandler | None = None
        self._pending: dict[int, list[_QueueEntry]] = {}
        self._entry_counter = 0
        self._bot: Bot | None = None

    def set_bot(self, bot: Bot) -> None:
        """Inject the Bot instance used to send/edit queue indicator messages."""
        self._bot = bot

    def set_abort_handler(self, handler: AbortHandler) -> None:
        """Register a callback invoked for abort triggers *before* the lock."""
        self._abort_handler = handler

    def set_quick_command_handler(self, handler: QuickCommandHandler) -> None:
        """Register a callback for read-only commands dispatched *before* the lock."""
        self._quick_command_handler = handler

    def get_lock(self, chat_id: int) -> asyncio.Lock:
        """Return the per-chat lock, creating it if needed.

        Used by webhook wake dispatch to queue behind active conversations.
        """
        if chat_id not in self._locks:
            if len(self._locks) >= _MAX_LOCKS:
                idle = [k for k, v in self._locks.items() if not v.locked()]
                for k in idle[: len(idle) // 2]:
                    del self._locks[k]
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    # -- Queue inspection & manipulation ---------------------------------------

    def has_pending(self, chat_id: int) -> bool:
        """Return True if *chat_id* has messages waiting in the queue."""
        return bool(self._pending.get(chat_id))

    def is_busy(self, chat_id: int) -> bool:
        """Return True if *chat_id* has the lock held or pending messages."""
        lock = self._locks.get(chat_id)
        if lock and lock.locked():
            return True
        return self.has_pending(chat_id)

    async def cancel_entry(self, chat_id: int, entry_id: int) -> bool:
        """Cancel a single queued message and edit its indicator.

        Returns True if the entry was found and cancelled.
        """
        entries = self._pending.get(chat_id, [])
        for entry in entries:
            if entry.entry_id == entry_id and not entry.cancelled:
                entry.cancelled = True
                await self._edit_indicator(chat_id, entry, "<i>[Message cancelled.]</i>")
                logger.info("Queue entry cancelled chat=%d entry=%d", chat_id, entry_id)
                return True
        return False

    async def drain_pending(self, chat_id: int) -> int:
        """Cancel ALL pending messages for *chat_id* and edit their indicators.

        Returns the number of entries discarded.
        """
        entries = self._pending.get(chat_id, [])
        count = 0
        for entry in entries:
            if not entry.cancelled:
                entry.cancelled = True
                await self._edit_indicator(chat_id, entry, "<i>[Message discarded.]</i>")
                count += 1
        logger.info("Queue drained chat=%d discarded=%d", chat_id, count)
        return count

    # -- Middleware entry point ------------------------------------------------

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.chat:
            return await handler(event, data)

        set_log_context(
            operation="msg",
            chat_id=event.chat.id if hasattr(event, "chat") else None,
        )

        chat_id = event.chat.id
        text = (event.text or "").strip()

        if self._abort_handler and text and is_abort_message(text):
            logger.debug("Abort trigger detected text=%s", text[:40])
            handled = await self._abort_handler(chat_id, event)
            if handled:
                await self.drain_pending(chat_id)
                return None

        if self._quick_command_handler and text and is_quick_command(text):
            logger.debug("Quick command bypass cmd=%s", text)
            handled = await self._quick_command_handler(chat_id, event)
            if handled:
                return None

        key = build_dedup_key(chat_id, event.message_id)
        if self._dedup.check(key):
            logger.debug("Message deduplicated msg_id=%d", event.message_id)
            return None

        lock = self.get_lock(chat_id)
        entry: _QueueEntry | None = None

        if lock.locked():
            entry = self._create_entry(chat_id, event)
            self._pending.setdefault(chat_id, []).append(entry)
            await self._send_indicator(chat_id, entry, event)

        async with lock:
            if entry is not None:
                self._remove_entry(chat_id, entry)
                if entry.cancelled:
                    await self._delete_indicator(chat_id, entry)
                    return None
                await self._delete_indicator(chat_id, entry)
            return await handler(event, data)

    # -- Internal helpers ------------------------------------------------------

    def _create_entry(self, chat_id: int, event: Message) -> _QueueEntry:
        self._entry_counter += 1
        return _QueueEntry(
            entry_id=self._entry_counter,
            chat_id=chat_id,
            message_id=event.message_id,
            text_preview=(event.text or "")[:40],
        )

    def _remove_entry(self, chat_id: int, entry: _QueueEntry) -> None:
        entries = self._pending.get(chat_id)
        if entries is None:
            return
        with contextlib.suppress(ValueError):
            entries.remove(entry)
        if not entries:
            del self._pending[chat_id]

    async def _send_indicator(self, chat_id: int, entry: _QueueEntry, event: Message) -> None:
        if not self._bot:
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Cancel message",
                        callback_data=f"{MQ_PREFIX}{entry.entry_id}",
                    )
                ]
            ]
        )
        try:
            sent = await self._bot.send_message(
                chat_id,
                "<i>[Message in queue...]</i>",
                parse_mode=ParseMode.HTML,
                reply_parameters=ReplyParameters(
                    message_id=event.message_id,
                    allow_sending_without_reply=True,
                ),
                reply_markup=keyboard,
            )
            entry.indicator_msg_id = sent.message_id
        except Exception:
            logger.debug("Failed to send queue indicator", exc_info=True)

    async def _edit_indicator(self, chat_id: int, entry: _QueueEntry, html: str) -> None:
        if not self._bot or not entry.indicator_msg_id:
            return
        try:
            await self._bot.edit_message_text(
                text=html,
                chat_id=chat_id,
                message_id=entry.indicator_msg_id,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        except Exception:
            logger.debug("Failed to edit queue indicator", exc_info=True)

    async def _delete_indicator(self, chat_id: int, entry: _QueueEntry) -> None:
        if not self._bot or not entry.indicator_msg_id:
            return
        try:
            await self._bot.delete_message(chat_id, entry.indicator_msg_id)
        except Exception:
            logger.debug("Failed to delete queue indicator", exc_info=True)
