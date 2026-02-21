"""Typing indicator context manager for Telegram chats."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Self

from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

_TYPING_INTERVAL = 4.0


class TypingContext:
    """Repeats 'typing' chat action every few seconds until stopped."""

    def __init__(self, bot: Bot, chat_id: int, *, thread_id: int | None = None) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._task: asyncio.Task[None] | None = None

    async def _loop(self) -> None:
        try:
            while True:
                await self._bot.send_chat_action(
                    chat_id=self._chat_id,
                    action=ChatAction.TYPING,
                    message_thread_id=self._thread_id,
                )
                await asyncio.sleep(_TYPING_INTERVAL)
        except TelegramAPIError:
            logger.debug("Typing loop stopped unexpectedly", exc_info=True)

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
