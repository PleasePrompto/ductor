"""Typing indicator context manager for Telegram chats."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self

from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

_TYPING_INTERVAL = 4.0
_RETRY_DELAY = 1.0


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
                try:
                    await self._bot.send_chat_action(
                        chat_id=self._chat_id,
                        action=ChatAction.TYPING,
                        message_thread_id=self._thread_id,
                    )
                except TelegramAPIError:
                    logger.warning("Typing action failed; retrying", exc_info=True)
                    await asyncio.sleep(_RETRY_DELAY)
                    continue
                await asyncio.sleep(_TYPING_INTERVAL)
        except asyncio.CancelledError:
            raise

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *_: object) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
