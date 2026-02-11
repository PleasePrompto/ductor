"""Dependency-based task queue for cron jobs and webhooks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _QueuedTask:
    """A task waiting to acquire a dependency lock."""

    task_id: str
    task_label: str
    dependency: str
    queued_at: float = field(default_factory=lambda: asyncio.get_event_loop().time())


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
            queue = [t for t in queue if t.task_id != task_id]
            if queue:
                self._queues[dependency] = queue
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

    def get_queue_info(self, dependency: str) -> dict[str, object]:
        """Get current queue status for a dependency (diagnostics)."""
        return {
            "dependency": dependency,
            "locked": self._locks.get(dependency, asyncio.Lock()).locked(),
            "active_task": self._active.get(dependency),
            "queue_length": len(self._queues.get(dependency, [])),
            "queued_tasks": [
                {
                    "task_id": t.task_id,
                    "task_label": t.task_label,
                    "queued_seconds": asyncio.get_event_loop().time() - t.queued_at,
                }
                for t in self._queues.get(dependency, [])
            ],
        }

    def get_all_dependencies(self) -> list[str]:
        """Return all known dependency names."""
        return sorted(set(self._locks.keys()) | set(self._queues.keys()))


_dependency_queue: DependencyQueue | None = None


def get_dependency_queue() -> DependencyQueue:
    """Get or create the global dependency queue singleton."""
    global _dependency_queue  # noqa: PLW0603
    if _dependency_queue is None:
        _dependency_queue = DependencyQueue()
    return _dependency_queue
