"""Dependency-based task queue for cron jobs and webhooks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _QueuedTask:
    """A task waiting to acquire a dependency lock."""

    task_id: str
    task_label: str
    dependency: str


class DependencyQueue:
    """Manages dependency-based locks for cron tasks.

    Tasks with same dependency run sequentially (FIFO).
    Tasks without dependencies or with different dependencies run in parallel.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._queues: dict[str, list[_QueuedTask]] = {}
        self._active: dict[str, str] = {}
        self._state_lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(
        self,
        task_id: str,
        task_label: str,
        dependency: str | None,
    ) -> AsyncIterator[None]:
        """Acquire dependency lock, execute task, release.

        Usage::

            async with dep_queue.acquire(job_id, job_title, job.dependency):
                await execute_task()
        """
        if dependency is None:
            logger.debug("Task executing without dependency: %s", task_label)
            yield
            return

        lock = await self._get_or_create_lock(dependency)
        await self._enqueue_task(task_id, task_label, dependency)

        async with lock:
            await self._mark_active(dependency, task_id, task_label)
            try:
                logger.info(
                    "Task acquired dependency: task=%s dependency=%s",
                    task_label,
                    dependency,
                )
                yield
            finally:
                await self._mark_released(dependency, task_id, task_label)

    async def _get_or_create_lock(self, dependency: str) -> asyncio.Lock:
        async with self._state_lock:
            if dependency not in self._locks:
                self._locks[dependency] = asyncio.Lock()
                logger.debug("Created lock for dependency: %s", dependency)
            return self._locks[dependency]

    async def _enqueue_task(self, task_id: str, task_label: str, dependency: str) -> None:
        async with self._state_lock:
            if dependency not in self._queues:
                self._queues[dependency] = []

            queued_task = _QueuedTask(
                task_id=task_id,
                task_label=task_label,
                dependency=dependency,
            )
            self._queues[dependency].append(queued_task)

            position = len(self._queues[dependency])
            active_task = self._active.get(dependency, "?")
            logger.info(
                "Task queued: task=%s dependency=%s position=%d active=%s",
                task_label,
                dependency,
                position,
                active_task,
            )

    async def _mark_active(self, dependency: str, task_id: str, task_label: str) -> None:
        async with self._state_lock:
            queue = self._queues.get(dependency, [])
            # Remove only the first matching entry so that if two tasks share
            # the same task_id (possible after a rapid reschedule), the second
            # one is not inadvertently evicted from the queue.
            new_queue: list[_QueuedTask] = []
            removed = False
            for t in queue:
                if not removed and t.task_id == task_id:
                    removed = True
                else:
                    new_queue.append(t)
            if new_queue:
                self._queues[dependency] = new_queue
            else:
                self._queues.pop(dependency, None)
            self._active[dependency] = task_label

    async def _mark_released(self, dependency: str, _task_id: str, task_label: str) -> None:
        async with self._state_lock:
            if self._active.get(dependency) == task_label:
                self._active.pop(dependency, None)

            remaining = len(self._queues.get(dependency, []))
            logger.info(
                "Task released dependency: task=%s dependency=%s remaining_queue=%d",
                task_label,
                dependency,
                remaining,
            )


_dependency_queue: list[DependencyQueue | None] = [None]


def get_dependency_queue() -> DependencyQueue:
    """Get or create the global dependency queue singleton."""
    q = _dependency_queue[0]
    if q is None:
        q = DependencyQueue()
        _dependency_queue[0] = q
    return q
