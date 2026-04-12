"""Message queue for Matrix bot: dedup, pending task tracking, and drain.

Brings the Matrix transport closer to parity with Telegram's
ConversationMiddleware by preventing duplicate processing, tracking
in-flight message tasks, and cancelling queued work on /stop.
"""

from __future__ import annotations

import asyncio
import logging

from ductor_bot.messenger.telegram.dedup import DedupeCache

logger = logging.getLogger(__name__)


class MatrixMessageQueue:
    """Lightweight message queue for the Matrix bot.

    Responsibilities:
    - **Dedup**: prevent the same Matrix event from being processed twice
      (common during initial sync or gappy syncs).
    - **Pending task tracking**: keep references to spawned message-handling
      tasks per chat_id so they can be cancelled on /stop.
    - **Drain**: cancel all pending tasks for a chat, returning the count
      of tasks actually cancelled.
    """

    def __init__(self) -> None:
        self._dedup = DedupeCache()
        self._pending: dict[int, list[asyncio.Task[None]]] = {}

    # -- Dedup ---------------------------------------------------------------

    def is_duplicate(self, event_id: str) -> bool:
        """Return True if this event_id was already seen (duplicate)."""
        return self._dedup.check(event_id)

    # -- Pending task tracking -----------------------------------------------

    def track(self, *, chat_id: int, task: asyncio.Task[None]) -> None:
        """Register a spawned message-handling task for a chat."""
        self._pending.setdefault(chat_id, []).append(task)

    def pending_count(self, chat_id: int) -> int:
        """Return the number of pending (not yet done) tasks for a chat."""
        tasks = self._pending.get(chat_id)
        if not tasks:
            return 0
        # Prune completed tasks
        active = [t for t in tasks if not t.done()]
        self._pending[chat_id] = active
        return len(active)

    def is_busy(self, chat_id: int) -> bool:
        """Return True if there are pending tasks for this chat."""
        return self.pending_count(chat_id) > 0

    # -- Drain ---------------------------------------------------------------

    def drain(self, chat_id: int) -> int:
        """Cancel all pending tasks for a chat.

        Returns the number of tasks actually cancelled (skips already-done tasks).
        """
        tasks = self._pending.pop(chat_id, [])
        cancelled = 0
        for task in tasks:
            if not task.done():
                task.cancel()
                cancelled += 1
        logger.info("Queue drained chat=%d cancelled=%d", chat_id, cancelled)
        return cancelled
