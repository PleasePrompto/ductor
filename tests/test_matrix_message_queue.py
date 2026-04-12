"""Tests for Matrix bot message queueing, dedup, and drain-on-stop.

These test the MatrixMessageQueue class in isolation — no nio, no real bot.
Red/green TDD: these tests are written FIRST, then the implementation follows.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ductor_bot.messenger.matrix.message_queue import MatrixMessageQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(event_id: str = "$abc", sender: str = "@user:srv") -> SimpleNamespace:
    return SimpleNamespace(event_id=event_id, sender=sender)


def _make_room(room_id: str = "!room:srv") -> SimpleNamespace:
    return SimpleNamespace(room_id=room_id)


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

class TestDedup:
    def test_first_event_is_not_duplicate(self) -> None:
        q = MatrixMessageQueue()
        assert q.is_duplicate("$event1") is False

    def test_same_event_id_is_duplicate(self) -> None:
        q = MatrixMessageQueue()
        q.is_duplicate("$event1")
        assert q.is_duplicate("$event1") is True

    def test_different_event_ids_are_not_duplicates(self) -> None:
        q = MatrixMessageQueue()
        q.is_duplicate("$event1")
        assert q.is_duplicate("$event2") is False


# ---------------------------------------------------------------------------
# Pending task tracking
# ---------------------------------------------------------------------------

class TestPendingTasks:
    def test_no_pending_initially(self) -> None:
        q = MatrixMessageQueue()
        assert q.pending_count(chat_id=1) == 0

    def test_track_adds_task(self) -> None:
        q = MatrixMessageQueue()

        async def _noop() -> None:
            await asyncio.sleep(10)

        loop = asyncio.new_event_loop()
        task = loop.create_task(_noop())
        q.track(chat_id=1, task=task)
        assert q.pending_count(chat_id=1) == 1
        task.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()

    def test_track_multiple_tasks(self) -> None:
        q = MatrixMessageQueue()

        async def _noop() -> None:
            await asyncio.sleep(10)

        loop = asyncio.new_event_loop()
        t1 = loop.create_task(_noop())
        t2 = loop.create_task(_noop())
        q.track(chat_id=1, task=t1)
        q.track(chat_id=1, task=t2)
        assert q.pending_count(chat_id=1) == 2
        t1.cancel()
        t2.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()

    def test_tasks_for_different_chats_are_separate(self) -> None:
        q = MatrixMessageQueue()

        async def _noop() -> None:
            await asyncio.sleep(10)

        loop = asyncio.new_event_loop()
        t1 = loop.create_task(_noop())
        t2 = loop.create_task(_noop())
        q.track(chat_id=1, task=t1)
        q.track(chat_id=2, task=t2)
        assert q.pending_count(chat_id=1) == 1
        assert q.pending_count(chat_id=2) == 1
        t1.cancel()
        t2.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()

    def test_completed_tasks_are_pruned(self) -> None:
        """When pending_count is checked, finished tasks should not be counted."""
        q = MatrixMessageQueue()

        async def _instant() -> None:
            pass

        async def _run() -> None:
            task = asyncio.current_task()  # dummy
            real_task = asyncio.create_task(_instant())
            q.track(chat_id=1, task=real_task)
            await asyncio.sleep(0.05)  # let it finish
            assert q.pending_count(chat_id=1) == 0

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Drain (cancel pending tasks for a chat)
# ---------------------------------------------------------------------------

class TestDrain:
    def test_drain_cancels_pending_tasks(self) -> None:
        q = MatrixMessageQueue()

        async def _waiter() -> None:
            await asyncio.sleep(10)

        async def _run() -> None:
            t1 = asyncio.create_task(_waiter())
            t2 = asyncio.create_task(_waiter())
            q.track(chat_id=1, task=t1)
            q.track(chat_id=1, task=t2)
            count = q.drain(chat_id=1)
            assert count == 2
            # Give the event loop a tick to process cancellations
            await asyncio.sleep(0)
            assert t1.cancelled()
            assert t2.cancelled()

        asyncio.run(_run())

    def test_drain_returns_zero_when_no_pending(self) -> None:
        q = MatrixMessageQueue()
        assert q.drain(chat_id=1) == 0

    def test_drain_does_not_affect_other_chats(self) -> None:
        q = MatrixMessageQueue()

        async def _waiter() -> None:
            await asyncio.sleep(10)

        async def _run() -> None:
            t1 = asyncio.create_task(_waiter())
            t2 = asyncio.create_task(_waiter())
            q.track(chat_id=1, task=t1)
            q.track(chat_id=2, task=t2)
            q.drain(chat_id=1)
            assert q.pending_count(chat_id=2) == 1
            t2.cancel()
            await asyncio.sleep(0.05)

        asyncio.run(_run())

    def test_drain_skips_already_done_tasks(self) -> None:
        q = MatrixMessageQueue()

        async def _instant() -> None:
            pass

        async def _run() -> None:
            t1 = asyncio.create_task(_instant())
            await asyncio.sleep(0.01)  # let it finish
            t2 = asyncio.create_task(asyncio.sleep(10))
            q.track(chat_id=1, task=t1)
            q.track(chat_id=1, task=t2)
            count = q.drain(chat_id=1)
            assert count == 1  # only t2 was actually cancelled
            t2.cancel()
            await asyncio.sleep(0.01)

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# is_busy
# ---------------------------------------------------------------------------

class TestIsBusy:
    def test_not_busy_when_empty(self) -> None:
        q = MatrixMessageQueue()
        assert q.is_busy(chat_id=1) is False

    def test_busy_when_has_pending(self) -> None:
        q = MatrixMessageQueue()

        async def _run() -> None:
            t = asyncio.create_task(asyncio.sleep(10))
            q.track(chat_id=1, task=t)
            assert q.is_busy(chat_id=1) is True
            t.cancel()
            await asyncio.sleep(0.01)

        asyncio.run(_run())
