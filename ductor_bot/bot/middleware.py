"""Telegram bot middleware: auth filtering and sequential processing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from ductor_bot.bot.abort import is_abort_message
from ductor_bot.bot.dedup import DedupeCache, build_dedup_key
from ductor_bot.log_context import set_log_context

logger = logging.getLogger(__name__)

AbortHandler = Callable[[int, "Message"], Awaitable[bool]]
"""Async callback: (chat_id, message) -> handled?"""

QuickCommandHandler = Callable[[int, "Message"], Awaitable[bool]]
"""Async callback for read-only commands that bypass the per-chat lock."""

QUICK_COMMANDS: frozenset[str] = frozenset({"/status", "/memory", "/cron", "/diagnose"})


def is_quick_command(text: str) -> bool:
    """Return True if *text* is a read-only command that can bypass the lock."""
    return text.strip().lower() in QUICK_COMMANDS


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


class SequentialMiddleware(BaseMiddleware):
    """Outer middleware: dedup + per-chat lock ensures sequential processing."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._dedup = DedupeCache()
        self._abort_handler: AbortHandler | None = None
        self._quick_command_handler: QuickCommandHandler | None = None

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

        async with self.get_lock(chat_id):
            return await handler(event, data)
