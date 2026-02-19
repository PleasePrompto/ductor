"""Tests for AuthMiddleware and SequentialMiddleware."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message


def _make_message(chat_id: int = 1, user_id: int = 100, text: str = "hello") -> MagicMock:
    """Create a mock aiogram Message."""
    msg = MagicMock(spec=Message)
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.text = text
    msg.message_id = 1
    return msg


class TestAuthMiddleware:
    """Test user ID filtering middleware."""

    async def test_allowed_user_passes(self) -> None:
        from ductor_bot.bot.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100, 200})
        handler = AsyncMock(return_value="ok")
        msg = _make_message(user_id=100)

        result = await mw(handler, msg, {})
        handler.assert_called_once()
        assert result == "ok"

    async def test_blocked_user_dropped(self) -> None:
        from ductor_bot.bot.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100})
        handler = AsyncMock()
        msg = _make_message(user_id=999)

        result = await mw(handler, msg, {})
        handler.assert_not_called()
        assert result is None

    async def test_no_from_user_dropped(self) -> None:
        from ductor_bot.bot.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100})
        handler = AsyncMock()
        msg = _make_message()
        msg.from_user = None

        result = await mw(handler, msg, {})
        handler.assert_not_called()
        assert result is None

    async def test_non_message_event_passes(self) -> None:
        from ductor_bot.bot.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100})
        handler = AsyncMock(return_value="pass")
        event = MagicMock()  # Not a Message or CallbackQuery
        event.__class__ = type("Update", (), {})

        result = await mw(handler, event, {})
        handler.assert_called_once()
        assert result == "pass"


class TestSequentialMiddleware:
    """Test dedup + per-chat sequential lock."""

    async def test_sequential_processing(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        call_order: list[int] = []

        async def handler(_event: object, _data: dict[str, object]) -> None:
            call_order.append(1)
            await asyncio.sleep(0.01)
            call_order.append(2)

        msg = _make_message(chat_id=1)
        await mw(handler, msg, {})
        assert call_order == [1, 2]

    async def test_duplicate_message_dropped(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        handler = AsyncMock()
        msg = _make_message(chat_id=1)
        msg.message_id = 42

        # First call goes through
        await mw(handler, msg, {})
        assert handler.call_count == 1

        # Same message_id is deduped
        await mw(handler, msg, {})
        assert handler.call_count == 1

    async def test_abort_trigger_bypasses_lock(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        abort_handler = AsyncMock(return_value=True)
        mw.set_abort_handler(abort_handler)

        handler = AsyncMock()
        msg = _make_message(chat_id=1, text="stop")

        result = await mw(handler, msg, {})
        abort_handler.assert_called_once()
        handler.assert_not_called()
        assert result is None

    async def test_non_abort_reaches_handler(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        abort_handler = AsyncMock(return_value=False)
        mw.set_abort_handler(abort_handler)

        handler = AsyncMock(return_value="handled")
        msg = _make_message(chat_id=1, text="hello there")

        await mw(handler, msg, {})
        handler.assert_called_once()

    async def test_quick_command_bypasses_lock(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        quick_handler = AsyncMock(return_value=True)
        mw.set_quick_command_handler(quick_handler)

        handler = AsyncMock()
        msg = _make_message(chat_id=1, text="/status")

        result = await mw(handler, msg, {})
        quick_handler.assert_called_once_with(1, msg)
        handler.assert_not_called()
        assert result is None

    async def test_quick_command_while_lock_held(self) -> None:
        """Quick command responds immediately even while a CLI call holds the lock."""
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        results: list[str] = []

        async def quick_handler(_chat_id: int, _message: object) -> bool:
            results.append("quick")
            return True

        mw.set_quick_command_handler(quick_handler)

        lock_acquired = asyncio.Event()
        release = asyncio.Event()

        async def slow_handler(_event: object, _data: dict[str, object]) -> None:
            lock_acquired.set()
            await release.wait()
            results.append("slow")

        slow_msg = _make_message(chat_id=1, text="do something long")
        task = asyncio.create_task(mw(slow_handler, slow_msg, {}))
        await lock_acquired.wait()

        quick_msg = _make_message(chat_id=1, text="/cron")
        quick_msg.message_id = 2
        await mw(AsyncMock(), quick_msg, {})

        assert results == ["quick"]

        release.set()
        await task
        assert results == ["quick", "slow"]

    async def test_non_quick_command_blocks_normally(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        quick_handler = AsyncMock(return_value=True)
        mw.set_quick_command_handler(quick_handler)

        handler = AsyncMock(return_value="handled")
        msg = _make_message(chat_id=1, text="/new")

        await mw(handler, msg, {})
        quick_handler.assert_not_called()
        handler.assert_called_once()


class TestGetLock:
    """Tests for SequentialMiddleware.get_lock()."""

    def test_same_chat_returns_same_lock(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        lock_a = mw.get_lock(1)
        lock_b = mw.get_lock(1)
        assert lock_a is lock_b

    def test_different_chats_return_different_locks(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        lock_a = mw.get_lock(1)
        lock_b = mw.get_lock(2)
        assert lock_a is not lock_b

    def test_returns_asyncio_lock(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        lock = mw.get_lock(42)
        assert isinstance(lock, asyncio.Lock)

    async def test_lock_shared_with_middleware(self) -> None:
        """Lock returned by get_lock is the same one used by __call__."""
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        lock = mw.get_lock(1)
        acquired = asyncio.Event()
        release = asyncio.Event()

        async def slow_handler(_event: object, _data: dict[str, object]) -> None:
            acquired.set()
            await release.wait()

        msg = _make_message(chat_id=1)
        task = asyncio.create_task(mw(slow_handler, msg, {}))
        await acquired.wait()

        # Lock should be held by the middleware call
        assert lock.locked()

        release.set()
        await task
        assert not lock.locked()


class TestIsQuickCommand:
    """Unit tests for is_quick_command()."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("/status", True),
            ("/memory", True),
            ("/cron", True),
            ("/diagnose", True),
            ("/STATUS", True),
            ("  /status  ", True),
            ("/model", True),
            ("/model sonnet", True),
            ("/status@jhcoding_bot", True),
            ("/model@jhcoding_bot gpt-5.3-codex", True),
            ("/new", False),
            ("/stop", False),
            ("/restart", False),
            ("hello", False),
            ("", False),
        ],
    )
    def test_is_quick_command(self, text: str, expected: bool) -> None:
        from ductor_bot.bot.middleware import is_quick_command

        assert is_quick_command(text) == expected


class TestQueueManagement:
    """Tests for queue entry tracking and cancellation."""

    async def test_is_busy_when_lock_held(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        lock = mw.get_lock(1)

        assert not mw.is_busy(1)

        acquired = asyncio.Event()
        release = asyncio.Event()

        async def hold_lock() -> None:
            async with lock:
                acquired.set()
                await release.wait()

        task = asyncio.create_task(hold_lock())
        await acquired.wait()
        assert mw.is_busy(1)

        release.set()
        await task
        assert not mw.is_busy(1)

    async def test_has_pending_empty(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        assert not mw.has_pending(1)

    async def test_cancel_entry_marks_cancelled(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware, _QueueEntry

        mw = SequentialMiddleware()
        entry = _QueueEntry(entry_id=1, chat_id=10, message_id=100, text_preview="test")
        mw._pending.setdefault(10, []).append(entry)

        assert not entry.cancelled
        result = await mw.cancel_entry(10, 1)
        assert result is True
        assert entry.cancelled

    async def test_cancel_entry_unknown_returns_false(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        result = await mw.cancel_entry(10, 999)
        assert result is False

    async def test_drain_pending_cancels_all(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware, _QueueEntry

        mw = SequentialMiddleware()
        entries = [
            _QueueEntry(entry_id=i, chat_id=10, message_id=100 + i, text_preview=f"msg{i}")
            for i in range(3)
        ]
        mw._pending[10] = list(entries)

        count = await mw.drain_pending(10)
        assert count == 3
        assert all(e.cancelled for e in entries)

    async def test_drain_pending_skips_already_cancelled(self) -> None:
        from ductor_bot.bot.middleware import SequentialMiddleware, _QueueEntry

        mw = SequentialMiddleware()
        e1 = _QueueEntry(entry_id=1, chat_id=10, message_id=101, text_preview="a")
        e2 = _QueueEntry(entry_id=2, chat_id=10, message_id=102, text_preview="b", cancelled=True)
        mw._pending[10] = [e1, e2]

        count = await mw.drain_pending(10)
        assert count == 1

    async def test_cancelled_entry_skips_handler(self) -> None:
        """When a queued message is cancelled, the handler is not invoked."""
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        bot.delete_message = AsyncMock()
        mw.set_bot(bot)

        acquired = asyncio.Event()
        release = asyncio.Event()
        handler_calls: list[str] = []

        async def slow_handler(_event: object, _data: dict[str, object]) -> None:
            acquired.set()
            await release.wait()
            handler_calls.append("slow")

        async def normal_handler(_event: object, _data: dict[str, object]) -> None:
            handler_calls.append("normal")

        msg1 = _make_message(chat_id=1, text="first")
        msg1.message_id = 1
        task1 = asyncio.create_task(mw(slow_handler, msg1, {}))
        await acquired.wait()

        msg2 = _make_message(chat_id=1, text="second")
        msg2.message_id = 2
        task2 = asyncio.create_task(mw(normal_handler, msg2, {}))
        await asyncio.sleep(0.01)

        assert mw.has_pending(1)
        entries = mw._pending.get(1, [])
        assert len(entries) == 1
        await mw.cancel_entry(1, entries[0].entry_id)

        release.set()
        await task1
        await task2

        assert handler_calls == ["slow"]

    async def test_abort_drains_pending(self) -> None:
        """Abort trigger drains the pending queue."""
        from ductor_bot.bot.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        bot.edit_message_text = AsyncMock()
        bot.delete_message = AsyncMock()
        mw.set_bot(bot)

        abort_handler = AsyncMock(return_value=True)
        mw.set_abort_handler(abort_handler)

        acquired = asyncio.Event()
        release = asyncio.Event()
        handler_calls: list[str] = []

        async def slow_handler(_event: object, _data: dict[str, object]) -> None:
            acquired.set()
            await release.wait()
            handler_calls.append("slow")

        async def queued_handler(_event: object, _data: dict[str, object]) -> None:
            handler_calls.append("queued")

        msg1 = _make_message(chat_id=1, text="first")
        msg1.message_id = 1
        task1 = asyncio.create_task(mw(slow_handler, msg1, {}))
        await acquired.wait()

        msg2 = _make_message(chat_id=1, text="second")
        msg2.message_id = 2
        task2 = asyncio.create_task(mw(queued_handler, msg2, {}))
        await asyncio.sleep(0.01)

        stop_msg = _make_message(chat_id=1, text="stop")
        stop_msg.message_id = 3
        await mw(AsyncMock(), stop_msg, {})

        release.set()
        await task1
        await task2

        assert handler_calls == ["slow"]
        abort_handler.assert_called_once()
