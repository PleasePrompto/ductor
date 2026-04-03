"""Unified per-session lock pool shared by all transports and the message bus."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_MAX_LOCKS = 1000


class LockPool:
    """Single lock pool for all message sources.

    Replaces ``SequentialMiddleware._locks``, ``ApiServer._locks``, and
    the ad-hoc ``bot.sequential.get_lock()`` calls in result delivery.
    """

    def __init__(self, *, max_locks: int = _MAX_LOCKS) -> None:
        self._locks: dict[tuple[str, str | None], asyncio.Lock] = {}
        self._max = max_locks

    def get(self, key: tuple[str, str | None] | str) -> asyncio.Lock:
        """Return the lock for *key*, creating one if needed.

        Accepts either a ``(chat_id, topic_id)`` tuple or a plain
        ``chat_id`` string.
        """
        k = self._normalize(key)
        if k not in self._locks:
            self._evict_if_needed()
            self._locks[k] = asyncio.Lock()
        return self._locks[k]

    def is_locked(self, key: tuple[str, str | None] | str) -> bool:
        """Return True if the lock for *key* is currently held."""
        lock = self._locks.get(self._normalize(key))
        return lock.locked() if lock else False

    def any_locked_for_chat(self, chat_id: str) -> bool:
        """Return True if any topic-scoped lock for *chat_id* is held."""
        return any(lock.locked() for (cid, _), lock in self._locks.items() if cid == chat_id)

    def __len__(self) -> int:
        return len(self._locks)

    # -- Internal helpers ------------------------------------------------------

    @staticmethod
    def _normalize(key: tuple[str, str | None] | str) -> tuple[str, str | None]:
        return key if isinstance(key, tuple) else (key, None)

    def _evict_if_needed(self) -> None:
        if len(self._locks) < self._max:
            return
        idle = [k for k, v in self._locks.items() if not v.locked()]
        for k in idle[: max(1, len(idle) // 2)]:
            del self._locks[k]
