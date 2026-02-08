"""In-memory LRU cache with TTL for message deduplication."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 30.0
_DEFAULT_MAX_SIZE = 200


class DedupeCache:
    """In-memory LRU cache with TTL for message deduplication.

    Uses ``time.monotonic`` to avoid clock-drift issues.  The cache maintains
    insertion order (Python 3.7+ dict guarantee) for efficient oldest-first
    eviction.
    """

    __slots__ = ("_cache", "_max_size", "_ttl")

    def __init__(
        self,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_size: int = _DEFAULT_MAX_SIZE,
    ) -> None:
        self._ttl = max(0.0, ttl_seconds)
        self._max_size = max(1, max_size)
        self._cache: dict[str, float] = {}

    def check(self, key: str) -> bool:
        """Return ``True`` if *key* was already seen within TTL (duplicate).

        On the first call for a given key the entry is inserted and ``False``
        is returned.  Subsequent calls within the TTL window return ``True``
        and refresh the timestamp.
        """
        now = time.monotonic()
        existing = self._cache.get(key)

        if existing is not None and (self._ttl <= 0 or now - existing < self._ttl):
            del self._cache[key]
            self._cache[key] = now
            logger.debug("Dedup hit key=%s", key)
            return True

        self._cache[key] = now
        self._prune(now)
        return False

    def _prune(self, now: float) -> None:
        """Remove expired entries, then enforce *max_size*."""
        if self._ttl > 0:
            cutoff = now - self._ttl
            expired = [k for k, ts in self._cache.items() if ts < cutoff]
            for k in expired:
                del self._cache[k]

        while len(self._cache) > self._max_size:
            oldest = next(iter(self._cache))
            del self._cache[oldest]

    def clear(self) -> None:
        """Drop all entries."""
        self._cache.clear()

    @property
    def size(self) -> int:
        """Number of entries currently in the cache."""
        return len(self._cache)


def build_dedup_key(chat_id: int, message_id: int) -> str:
    """Build a dedup key from Telegram's native identifiers."""
    return f"{chat_id}:{message_id}"
