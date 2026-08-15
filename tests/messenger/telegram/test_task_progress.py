"""Tests for Telegram's single-message TaskHub progress tracker."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from ductor_bot.messenger.telegram.task_progress import (
    TaskProgressUpdate,
    TelegramTaskProgressTracker,
)


def _message(message_id: int) -> MagicMock:
    message = MagicMock()
    message.message_id = message_id
    return message


def _bot(*, message_ids: list[int] | None = None) -> MagicMock:
    bot = MagicMock()
    ids = iter(message_ids or [101])
    bot.send_message = AsyncMock(side_effect=lambda **_: _message(next(ids)))
    bot.edit_message_text = AsyncMock()
    return bot


async def _wait_until(predicate: object, limit_seconds: float = 0.5) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_seconds
    while not callable(predicate) or not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.002)


async def _running(
    tracker: TelegramTaskProgressTracker,
    *,
    chat_id: int = 10,
    topic_id: int | None = 20,
    task_id: str = "task-1",
    name: str = "Read-only investigation",
) -> None:
    await tracker.update(
        TaskProgressUpdate(
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task_id,
            name=name,
            stage="running",
            elapsed_seconds=0.0,
        )
    )


class TestTelegramTaskProgressTracker:
    async def test_running_sends_plain_status_message_to_origin_topic(self) -> None:
        bot = _bot()
        tracker = TelegramTaskProgressTracker(bot, interval_seconds=30.0)

        await _running(tracker, name="<thinking>must not be parsed</thinking>")

        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == 10
        assert kwargs["message_thread_id"] == 20
        assert kwargs["parse_mode"] is None
        assert "running" in kwargs["text"]
        assert "Prompt:" not in kwargs["text"]
        await tracker.shutdown()

    async def test_heartbeat_edits_the_original_message_without_spam(self) -> None:
        bot = _bot(message_ids=[101])
        tracker = TelegramTaskProgressTracker(bot, interval_seconds=0.01)

        await _running(tracker)
        await _wait_until(lambda: bot.edit_message_text.await_count >= 1)

        assert bot.send_message.await_count == 1
        assert {call.kwargs["message_id"] for call in bot.edit_message_text.await_args_list} == {
            101
        }
        await tracker.shutdown()

    async def test_reviewing_keeps_the_same_heartbeat_message_alive(self) -> None:
        bot = _bot(message_ids=[101])
        tracker = TelegramTaskProgressTracker(bot, interval_seconds=0.01)

        await _running(tracker)
        await tracker.update(
            TaskProgressUpdate(
                chat_id=10,
                topic_id=20,
                task_id="task-1",
                name="Read-only investigation",
                stage="reviewing",
                elapsed_seconds=12.0,
            )
        )
        await _wait_until(lambda: bot.edit_message_text.await_count >= 2)

        assert bot.send_message.await_count == 1
        assert bot.edit_message_text.await_args.kwargs["message_id"] == 101
        assert "reviewing" in bot.edit_message_text.await_args.kwargs["text"]
        await tracker.shutdown()

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("done", "completed"),
            ("failed", "failed"),
            ("cancelled", "cancelled"),
            ("waiting", "waiting"),
            ("timeout", "timed out"),
            ("unexpected", "finished"),
        ],
    )
    async def test_terminal_status_stops_heartbeat_and_edits_terminal_text(
        self, status: str, expected: str
    ) -> None:
        bot = _bot(message_ids=[101])
        tracker = TelegramTaskProgressTracker(bot, interval_seconds=0.01)

        await _running(tracker)
        handled = await tracker.finish(
            chat_id=10,
            topic_id=20,
            task_id="task-1",
            status=status,
        )
        count_after_finish = bot.edit_message_text.await_count
        await asyncio.sleep(0.04)

        assert handled is True
        assert expected in bot.edit_message_text.await_args.kwargs["text"]
        assert bot.edit_message_text.await_count == count_after_finish
        await tracker.shutdown()

    async def test_tasks_are_isolated_by_chat_topic_and_task_id(self) -> None:
        bot = _bot(message_ids=[101, 102, 103])
        tracker = TelegramTaskProgressTracker(bot, interval_seconds=30.0)

        await _running(tracker, chat_id=10, topic_id=20, task_id="one")
        await _running(tracker, chat_id=10, topic_id=21, task_id="one")
        await _running(tracker, chat_id=11, topic_id=20, task_id="one")
        await tracker.update(
            TaskProgressUpdate(
                chat_id=10,
                topic_id=20,
                task_id="one",
                name="Read-only investigation",
                stage="reviewing",
                elapsed_seconds=1.0,
            )
        )

        assert len(tracker._states) == 3
        assert bot.edit_message_text.await_args.kwargs["message_id"] == 101
        await tracker.shutdown()

    async def test_uneditable_status_message_is_replaced_once(self) -> None:
        bot = _bot(message_ids=[101, 202])
        bot.edit_message_text = AsyncMock(
            side_effect=TelegramBadRequest(MagicMock(), "message to edit not found")
        )
        tracker = TelegramTaskProgressTracker(bot, interval_seconds=30.0)

        await _running(tracker)
        await tracker.update(
            TaskProgressUpdate(
                chat_id=10,
                topic_id=20,
                task_id="task-1",
                name="Read-only investigation",
                stage="reviewing",
                elapsed_seconds=1.0,
            )
        )
        await tracker.update(
            TaskProgressUpdate(
                chat_id=10,
                topic_id=20,
                task_id="task-1",
                name="Read-only investigation",
                stage="reviewing",
                elapsed_seconds=2.0,
            )
        )

        assert bot.send_message.await_count == 2
        assert tracker._states[(10, 20, "task-1")].message_id == 202
        await tracker.shutdown()

    async def test_rate_limit_retries_one_edit_of_the_same_message(self) -> None:
        bot = _bot(message_ids=[101])
        bot.edit_message_text = AsyncMock(
            side_effect=[TelegramRetryAfter(MagicMock(), "retry", retry_after=0), None]
        )
        tracker = TelegramTaskProgressTracker(bot, interval_seconds=30.0)

        await _running(tracker)
        await tracker.update(
            TaskProgressUpdate(
                chat_id=10,
                topic_id=20,
                task_id="task-1",
                name="Read-only investigation",
                stage="reviewing",
                elapsed_seconds=1.0,
            )
        )

        assert bot.edit_message_text.await_count == 2
        assert {call.kwargs["message_id"] for call in bot.edit_message_text.await_args_list} == {
            101
        }
        await tracker.shutdown()

    async def test_terminal_finish_wins_over_a_late_heartbeat_edit(self) -> None:
        bot = _bot(message_ids=[101])
        edit_started = asyncio.Event()
        release_edit = asyncio.Event()

        async def _block_first_edit(**_: object) -> None:
            edit_started.set()
            await release_edit.wait()

        bot.edit_message_text = AsyncMock(side_effect=_block_first_edit)
        tracker = TelegramTaskProgressTracker(bot, interval_seconds=0.01)

        await _running(tracker)
        await asyncio.wait_for(edit_started.wait(), timeout=0.5)
        finish = asyncio.create_task(
            tracker.finish(chat_id=10, topic_id=20, task_id="task-1", status="done")
        )
        await asyncio.sleep(0)
        release_edit.set()
        assert await asyncio.wait_for(finish, timeout=0.5) is True

        assert "completed" in bot.edit_message_text.await_args.kwargs["text"]
        await tracker.shutdown()

    async def test_resume_same_task_id_starts_a_new_tracker_generation(self) -> None:
        bot = _bot(message_ids=[101, 202])
        tracker = TelegramTaskProgressTracker(bot, interval_seconds=30.0)

        await _running(tracker)
        assert await tracker.finish(chat_id=10, topic_id=20, task_id="task-1", status="done")
        await _running(tracker)

        assert bot.send_message.await_count == 2
        assert tracker._states[(10, 20, "task-1")].message_id == 202
        await tracker.shutdown()

    async def test_later_progress_retries_an_initial_send_failure(self) -> None:
        bot = _bot(message_ids=[202])
        bot.send_message = AsyncMock(
            side_effect=[RuntimeError("network unavailable"), _message(202)]
        )
        tracker = TelegramTaskProgressTracker(bot, interval_seconds=30.0)

        await _running(tracker)
        await tracker.update(
            TaskProgressUpdate(
                chat_id=10,
                topic_id=20,
                task_id="task-1",
                name="Read-only investigation",
                stage="reviewing",
                elapsed_seconds=1.0,
            )
        )

        assert bot.send_message.await_count == 2
        assert tracker._states[(10, 20, "task-1")].message_id == 202
        await tracker.shutdown()

    async def test_shutdown_cancels_all_heartbeats_without_more_edits(self) -> None:
        bot = _bot(message_ids=[101])
        tracker = TelegramTaskProgressTracker(bot, interval_seconds=0.01)

        await _running(tracker)
        await tracker.shutdown()
        count_after_shutdown = bot.edit_message_text.await_count
        await asyncio.sleep(0.04)

        assert tracker._states == {}
        assert bot.edit_message_text.await_count == count_after_shutdown
