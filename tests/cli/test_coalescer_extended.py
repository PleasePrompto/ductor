"""Extended coalescer tests -- idle timer and edge cases."""

from __future__ import annotations

import asyncio

from ductor_bot.cli.coalescer import CoalesceConfig, StreamCoalescer


async def test_idle_timer_fires_flush() -> None:
    """Feed text above min_chars but below max_chars, wait for idle timer to fire."""
    flushed: list[str] = []

    async def on_flush(text: str) -> None:
        flushed.append(text)

    config = CoalesceConfig(min_chars=5, max_chars=1000, idle_ms=50)
    c = StreamCoalescer(config, on_flush)

    await c.feed("Hello world")  # Above min_chars, below max_chars, no sentence/paragraph break
    assert flushed == []  # Not yet flushed

    # Wait for idle timer to fire
    await asyncio.sleep(0.15)  # 150ms > 50ms idle_ms
    assert len(flushed) == 1
    assert flushed[0] == "Hello world"

    c.stop()


async def test_idle_timer_cancelled_by_new_feed() -> None:
    """Feeding new text resets the idle timer."""
    flushed: list[str] = []

    async def on_flush(text: str) -> None:
        flushed.append(text)

    config = CoalesceConfig(min_chars=5, max_chars=1000, idle_ms=100)
    c = StreamCoalescer(config, on_flush)

    await c.feed("Hello ")
    await asyncio.sleep(0.05)  # 50ms < 100ms
    await c.feed("world")  # Resets timer
    await asyncio.sleep(0.05)  # Still below 100ms from last feed
    assert flushed == []  # Timer was cancelled and reset

    await asyncio.sleep(0.15)  # Now past idle threshold
    assert len(flushed) == 1
    assert flushed[0] == "Hello world"

    c.stop()


async def test_feed_empty_string_no_crash() -> None:
    flushed: list[str] = []

    async def on_flush(text: str) -> None:
        flushed.append(text)

    c = StreamCoalescer(CoalesceConfig(min_chars=10, max_chars=100, idle_ms=5000), on_flush)
    await c.feed("")
    assert flushed == []
    c.stop()


async def test_flush_reentrant_guard() -> None:
    """Concurrent flushes should not corrupt buffer."""
    flushed: list[str] = []

    async def slow_flush(text: str) -> None:
        await asyncio.sleep(0.01)
        flushed.append(text)

    c = StreamCoalescer(CoalesceConfig(min_chars=1, max_chars=10, idle_ms=5000), slow_flush)
    await c.feed("x" * 15)  # Triggers max_chars flush
    assert len(flushed) == 1
    c.stop()
