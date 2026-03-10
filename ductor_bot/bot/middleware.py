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

from ductor_bot.bot.abort import is_abort_all_message, is_abort_message, is_interrupt_message
from ductor_bot.bot.dedup import DedupeCache, build_dedup_key
from ductor_bot.bot.topic import TopicNameCache, get_session_key, get_thread_id
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.log_context import set_log_context

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

AbortHandler = Callable[[int, "Message"], Awaitable[bool]]
"""Async callback: (chat_id, message) -> handled?"""

AbortAllHandler = Callable[[int, "Message"], Awaitable[bool]]
"""Async callback for /stop_all: (chat_id, message) -> handled?"""

QuickCommandHandler = Callable[[int, "Message"], Awaitable[bool]]
"""Async callback for read-only commands that bypass the per-chat lock."""

QUICK_COMMANDS: frozenset[str] = frozenset(
    {
        "/status",
        "/memory",
        "/cron",
        "/diagnose",
        "/model",
        "/showfiles",
        "/sessions",
        "/tasks",
        "/where",
        "/leave",
    }
)

MQ_PREFIX = "mq:"
"""Callback data prefix for message queue cancel buttons."""


def is_quick_command(text: str) -> bool:
    """Return True if *text* is a command that can bypass the lock.

    Matches bare commands (``/status``), bot-mentioned commands
    (``/status@my_bot``), and commands with arguments (``/model sonnet``).
    """
    cmd = text.strip().lower().split(None, 1)[0] if text.strip() else ""
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    return cmd in QUICK_COMMANDS


RejectedCallback = Callable[[int, str, str], None]
"""Sync callback for rejected group messages: (chat_id, chat_type, title)."""


class AuthMiddleware(BaseMiddleware):
    """Outer middleware: silently drop messages from unauthorized users/groups.

    In private chats only ``allowed_user_ids`` is checked.
    In group/supergroup chats **both** the group (``allowed_group_ids``)
    and the sender (``allowed_user_ids``) must be allowlisted.
    """

    def __init__(
        self,
        allowed_user_ids: set[int],
        *,
        allowed_group_ids: set[int] | None = None,
        on_rejected: RejectedCallback | None = None,
    ) -> None:
        self._allowed_users = allowed_user_ids
        self._allowed_groups = allowed_group_ids or set()
        self._on_rejected = on_rejected

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

        if not user:
            return None

        # Resolve chat: Message.chat directly, CallbackQuery via .message.chat.
        chat = None
        if isinstance(event, Message):
            chat = event.chat
        elif isinstance(event, CallbackQuery) and event.message is not None:
            chat = getattr(event.message, "chat", None)

        chat_type = chat.type if chat else None

        if chat_type in ("group", "supergroup"):
            group_id = chat.id if chat else None
            if group_id not in self._allowed_groups:
                if self._on_rejected and chat:
                    self._on_rejected(chat.id, chat_type, chat.title or "")
                return None
            if user.id not in self._allowed_users:
                return None
        elif user.id not in self._allowed_users:
            return None

        return await handler(event, data)


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

    def __init__(
        self,
        lock_pool: LockPool | None = None,
        topic_names: TopicNameCache | None = None,
    ) -> None:
        self._lock_pool = lock_pool if lock_pool is not None else LockPool()
        self._topic_names = topic_names
        self._dedup = DedupeCache()
        self._interrupt_handler: AbortHandler | None = None
        self._abort_handler: AbortHandler | None = None
        self._abort_all_handler: AbortAllHandler | None = None
        self._quick_command_handler: QuickCommandHandler | None = None
        self._pending: dict[int, list[_QueueEntry]] = {}
        self._entry_counter = 0
        self._bot: Bot | None = None

    @property
    def lock_pool(self) -> LockPool:
        """The underlying lock pool (shared with the message bus)."""
        return self._lock_pool

    def set_bot(self, bot: Bot) -> None:
        """Inject the Bot instance used to send/edit queue indicator messages."""
        self._bot = bot

    def set_interrupt_handler(self, handler: AbortHandler) -> None:
        """Register a callback invoked for interrupt triggers *before* the lock."""
        self._interrupt_handler = handler

    def set_abort_handler(self, handler: AbortHandler) -> None:
        """Register a callback invoked for abort triggers *before* the lock."""
        self._abort_handler = handler

    def set_abort_all_handler(self, handler: AbortAllHandler) -> None:
        """Register a callback invoked for 'stop all' triggers *before* the lock."""
        self._abort_all_handler = handler

    def set_quick_command_handler(self, handler: QuickCommandHandler) -> None:
        """Register a callback for read-only commands dispatched *before* the lock."""
        self._quick_command_handler = handler

    def get_lock(self, lock_key: tuple[int, int | None] | int) -> asyncio.Lock:
        """Return the per-session lock, creating it if needed.

        Accepts either a ``(chat_id, topic_id)`` tuple (from
        ``SessionKey.lock_key``) or a plain ``chat_id`` integer for
        backward compatibility.

        Used by webhook wake dispatch to queue behind active conversations.
        """
        return self._lock_pool.get(lock_key)

    # -- Queue inspection & manipulation ---------------------------------------

    def has_pending(self, chat_id: int) -> bool:
        """Return True if *chat_id* has messages waiting in the queue."""
        return bool(self._pending.get(chat_id))

    def is_busy(self, chat_id: int) -> bool:
        """Return True if *chat_id* has any lock held or pending messages.

        Checks all topic-scoped locks for the given chat.
        """
        return self._lock_pool.any_locked_for_chat(chat_id) or self.has_pending(chat_id)

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

    async def _check_abort(self, chat_id: int, text: str, event: Message) -> bool:
        """Check for interrupt, abort-all and abort triggers. Returns True if handled."""
        # Check interrupt FIRST — "esc" and "interrupt" trigger soft SIGINT,
        # not a full kill.  Must come before abort which would match too.
        if self._interrupt_handler and is_interrupt_message(text):
            logger.debug("Interrupt trigger detected text=%s", text[:40])
            handled = await self._interrupt_handler(chat_id, event)
            if handled:
                return True

        # Check "stop all" BEFORE "stop" — "stop all" contains "stop"
        if self._abort_all_handler and is_abort_all_message(text):
            logger.debug("Abort-all trigger detected text=%s", text[:40])
            handled = await self._abort_all_handler(chat_id, event)
            if handled:
                await self.drain_pending(chat_id)
                return True

        if self._abort_handler and is_abort_message(text):
            logger.debug("Abort trigger detected text=%s", text[:40])
            handled = await self._abort_handler(chat_id, event)
            if handled:
                await self.drain_pending(chat_id)
                return True

        return False

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.chat:
            return await handler(event, data)

        topic_label: str | None = None
        if event.is_topic_message and event.message_thread_id and self._topic_names:
            topic_label = self._topic_names.resolve(event.chat.id, event.message_thread_id)

        set_log_context(
            operation="msg",
            chat_id=event.chat.id if hasattr(event, "chat") else None,
            topic=topic_label,
        )

        chat_id = event.chat.id
        text = (event.text or "").strip()

        if text and await self._check_abort(chat_id, text, event):
            return None

        if self._quick_command_handler and text and is_quick_command(text):
            logger.debug("Quick command bypass cmd=%s", text)
            handled = await self._quick_command_handler(chat_id, event)
            if handled:
                return None

        dedup_key = build_dedup_key(chat_id, event.message_id)
        if self._dedup.check(dedup_key):
            logger.debug("Message deduplicated msg_id=%d", event.message_id)
            return None

        session_key = get_session_key(event)
        lock = self.get_lock(session_key.lock_key)
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
                message_thread_id=get_thread_id(event),
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
