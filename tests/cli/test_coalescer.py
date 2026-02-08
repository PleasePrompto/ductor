"""Tests for stream text coalescer."""

from __future__ import annotations

import pytest

from ductor_bot.cli.coalescer import CoalesceConfig, StreamCoalescer


@pytest.fixture
def flushed() -> list[str]:
    """Accumulates flushed text chunks."""
    return []


def _make_coalescer(
    flushed: list[str],
    *,
    min_chars: int = 10,
    max_chars: int = 100,
    idle_ms: int = 5000,
    paragraph_break: bool = True,
    sentence_break: bool = True,
) -> StreamCoalescer:
    async def on_flush(text: str) -> None:
        flushed.append(text)

    config = CoalesceConfig(
        min_chars=min_chars,
        max_chars=max_chars,
        idle_ms=idle_ms,
        paragraph_break=paragraph_break,
        sentence_break=sentence_break,
    )
    return StreamCoalescer(config, on_flush)


async def test_flush_on_max_chars(flushed: list[str]) -> None:
    c = _make_coalescer(flushed, max_chars=20)
    await c.feed("x" * 25)
    assert len(flushed) == 1
    assert len(flushed[0]) == 25


async def test_no_flush_below_min_chars(flushed: list[str]) -> None:
    c = _make_coalescer(flushed, min_chars=50)
    await c.feed("short")
    assert flushed == []


async def test_flush_on_paragraph_break(flushed: list[str]) -> None:
    c = _make_coalescer(flushed, min_chars=5)
    await c.feed("Hello world.\n\nNew paragraph.")
    assert len(flushed) == 1
    assert flushed[0].endswith("\n\n")


async def test_flush_on_sentence_break(flushed: list[str]) -> None:
    c = _make_coalescer(flushed, min_chars=5, paragraph_break=False)
    await c.feed("First sentence. Second part")
    assert len(flushed) == 1
    assert "First sentence. " in flushed[0]


async def test_force_flush(flushed: list[str]) -> None:
    c = _make_coalescer(flushed, min_chars=1000)
    await c.feed("small")
    await c.flush(force=True)
    assert len(flushed) == 1
    assert flushed[0] == "small"


async def test_flush_without_force_respects_min_chars(flushed: list[str]) -> None:
    c = _make_coalescer(flushed, min_chars=100)
    await c.feed("small")
    await c.flush()
    assert flushed == []


async def test_stop_cancels_timer(flushed: list[str]) -> None:
    c = _make_coalescer(flushed)
    await c.feed("x" * 15)
    c.stop()
    # No error on stop


async def test_coalesce_config_defaults() -> None:
    cfg = CoalesceConfig()
    assert cfg.min_chars == 200
    assert cfg.max_chars == 4000
    assert cfg.idle_ms == 800
    assert cfg.paragraph_break is True
    assert cfg.sentence_break is True


async def test_multiple_feeds_accumulate(flushed: list[str]) -> None:
    c = _make_coalescer(flushed, min_chars=20, max_chars=100)
    await c.feed("Hello ")
    await c.feed("world")
    assert flushed == []
    await c.flush(force=True)
    assert flushed == ["Hello world"]
