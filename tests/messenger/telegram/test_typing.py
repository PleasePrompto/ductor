"""Tests for TypingContext (typing indicator)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramNetworkError


class TestTypingContext:
    """Test the typing indicator context manager."""

    async def test_sends_typing_action_on_enter(self) -> None:
        from ductor_bot.messenger.telegram.typing import TypingContext

        bot = MagicMock()
        bot.send_chat_action = AsyncMock()
        ctx = TypingContext(bot, chat_id=42)

        async with ctx:
            # Give the background task a chance to run
            await asyncio.sleep(0.05)

        bot.send_chat_action.assert_called()
        args = bot.send_chat_action.call_args
        assert args.kwargs.get("chat_id") == 42
        assert args.kwargs.get("action") == ChatAction.TYPING

    async def test_cancels_loop_on_exit(self) -> None:
        from ductor_bot.messenger.telegram.typing import TypingContext

        bot = MagicMock()
        bot.send_chat_action = AsyncMock()
        ctx = TypingContext(bot, chat_id=1)

        async with ctx:
            await asyncio.sleep(0.01)

        # After exit, the internal task should be cancelled
        assert ctx._task is None or ctx._task.done()

    async def test_no_error_on_double_exit(self) -> None:
        from ductor_bot.messenger.telegram.typing import TypingContext

        bot = MagicMock()
        bot.send_chat_action = AsyncMock()
        ctx = TypingContext(bot, chat_id=1)

        async with ctx:
            pass
        # Manually calling __aexit__ again should not raise
        await ctx.__aexit__()

    async def test_thread_id_passed_to_send_chat_action(self) -> None:
        from ductor_bot.messenger.telegram.typing import TypingContext

        bot = MagicMock()
        bot.send_chat_action = AsyncMock()
        ctx = TypingContext(bot, chat_id=42, thread_id=99)

        async with ctx:
            await asyncio.sleep(0.05)

        assert bot.send_chat_action.call_args.kwargs["message_thread_id"] == 99

    async def test_thread_id_none_by_default(self) -> None:
        from ductor_bot.messenger.telegram.typing import TypingContext

        bot = MagicMock()
        bot.send_chat_action = AsyncMock()
        ctx = TypingContext(bot, chat_id=42)

        async with ctx:
            await asyncio.sleep(0.05)

        assert bot.send_chat_action.call_args.kwargs.get("message_thread_id") is None

    async def test_recovers_after_transient_telegram_failure(self, monkeypatch) -> None:
        from ductor_bot.messenger.telegram import typing as typing_module
        from ductor_bot.messenger.telegram.typing import TypingContext

        monkeypatch.setattr(typing_module, "_RETRY_BASE_SECONDS", 0.001)
        bot = MagicMock()
        bot.send_chat_action = AsyncMock(side_effect=[TelegramNetworkError(method=MagicMock(), message="x"), None])
        async with TypingContext(bot, chat_id=42):
            await asyncio.sleep(0.02)
        assert bot.send_chat_action.await_count >= 2

    async def test_cancellation_during_retry_is_prompt(self, monkeypatch) -> None:
        from ductor_bot.messenger.telegram import typing as typing_module
        from ductor_bot.messenger.telegram.typing import TypingContext

        monkeypatch.setattr(typing_module, "_RETRY_BASE_SECONDS", 30.0)
        bot = MagicMock()
        bot.send_chat_action = AsyncMock(side_effect=OSError("offline"))
        ctx = TypingContext(bot, chat_id=42)
        await ctx.__aenter__()
        await asyncio.sleep(0)
        await asyncio.wait_for(ctx.__aexit__(), timeout=0.1)
