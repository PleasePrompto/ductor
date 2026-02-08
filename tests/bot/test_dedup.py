"""Tests for DedupeCache and build_dedup_key."""

from __future__ import annotations

import time
from unittest.mock import patch


class TestDedupeCache:
    """Test LRU cache with TTL for message deduplication."""

    def test_first_check_returns_false(self) -> None:
        from ductor_bot.bot.dedup import DedupeCache

        cache = DedupeCache(ttl_seconds=10.0)
        assert cache.check("key1") is False

    def test_second_check_returns_true(self) -> None:
        from ductor_bot.bot.dedup import DedupeCache

        cache = DedupeCache(ttl_seconds=10.0)
        cache.check("key1")
        assert cache.check("key1") is True

    def test_different_keys_independent(self) -> None:
        from ductor_bot.bot.dedup import DedupeCache

        cache = DedupeCache(ttl_seconds=10.0)
        cache.check("key1")
        assert cache.check("key2") is False

    def test_expired_entry_returns_false(self) -> None:
        from ductor_bot.bot.dedup import DedupeCache

        cache = DedupeCache(ttl_seconds=1.0)
        now = time.monotonic()

        with patch("ductor_bot.bot.dedup.time") as mock_time:
            mock_time.monotonic.return_value = now
            cache.check("key1")

            # Fast-forward past TTL
            mock_time.monotonic.return_value = now + 2.0
            assert cache.check("key1") is False

    def test_max_size_evicts_oldest(self) -> None:
        from ductor_bot.bot.dedup import DedupeCache

        cache = DedupeCache(ttl_seconds=60.0, max_size=3)
        cache.check("a")
        cache.check("b")
        cache.check("c")
        cache.check("d")  # Should evict "a"
        assert cache.size <= 3

    def test_clear_empties_cache(self) -> None:
        from ductor_bot.bot.dedup import DedupeCache

        cache = DedupeCache()
        cache.check("a")
        cache.check("b")
        cache.clear()
        assert cache.size == 0

    def test_size_property(self) -> None:
        from ductor_bot.bot.dedup import DedupeCache

        cache = DedupeCache()
        assert cache.size == 0
        cache.check("a")
        assert cache.size == 1

    def test_duplicate_refreshes_timestamp(self) -> None:
        from ductor_bot.bot.dedup import DedupeCache

        cache = DedupeCache(ttl_seconds=5.0)
        now = time.monotonic()

        with patch("ductor_bot.bot.dedup.time") as mock_time:
            mock_time.monotonic.return_value = now
            cache.check("key1")

            # 3 seconds later: refresh by checking again
            mock_time.monotonic.return_value = now + 3.0
            assert cache.check("key1") is True

            # 3 more seconds (6 total, but only 3 since refresh)
            mock_time.monotonic.return_value = now + 6.0
            assert cache.check("key1") is True  # Still within TTL from refresh

    def test_min_max_size_clamped(self) -> None:
        from ductor_bot.bot.dedup import DedupeCache

        cache = DedupeCache(max_size=0)  # Should clamp to 1
        cache.check("a")
        cache.check("b")
        assert cache.size == 1

    def test_zero_ttl_always_duplicate(self) -> None:
        from ductor_bot.bot.dedup import DedupeCache

        cache = DedupeCache(ttl_seconds=0.0)
        cache.check("key1")
        assert cache.check("key1") is True


class TestBuildDedupKey:
    """Test dedup key construction."""

    def test_key_format(self) -> None:
        from ductor_bot.bot.dedup import build_dedup_key

        assert build_dedup_key(123, 456) == "123:456"

    def test_different_chat_different_key(self) -> None:
        from ductor_bot.bot.dedup import build_dedup_key

        assert build_dedup_key(1, 100) != build_dedup_key(2, 100)
