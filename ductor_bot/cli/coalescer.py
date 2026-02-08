"""Text coalescing buffer with idle timer for streaming output."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SENTENCE_END_RE = re.compile(r"[.!?][\s\n]")


@dataclass(frozen=True, slots=True)
class CoalesceConfig:
    """Tuning knobs for the stream coalescer."""

    min_chars: int = 200
    max_chars: int = 4000
    idle_ms: int = 800
    paragraph_break: bool = True
    sentence_break: bool = True


class StreamCoalescer:
    """Buffer streaming text and flush at readable boundaries."""

    def __init__(
        self,
        config: CoalesceConfig,
        on_flush: Callable[[str], Awaitable[None]],
    ) -> None:
        self._config = config
        self._on_flush = on_flush
        self._buffer = ""
        self._idle_handle: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._flushing = False

    async def feed(self, text: str) -> None:
        """Add text to the buffer. May trigger a flush."""
        self._buffer += text
        self._cancel_idle()

        buf_len = len(self._buffer)

        if buf_len >= self._config.max_chars:
            await self._do_flush()
            return

        if (
            self._config.paragraph_break
            and "\n\n" in self._buffer
            and buf_len >= self._config.min_chars
        ):
            pos = self._buffer.rfind("\n\n")
            await self._do_flush_up_to(pos + 2)
            return

        if self._config.sentence_break and buf_len >= self._config.min_chars:
            sentence_pos = self._find_sentence_break()
            if sentence_pos is not None:
                await self._do_flush_up_to(sentence_pos)
                return

        if buf_len >= self._config.min_chars:
            self._start_idle()

    async def flush(self, *, force: bool = False) -> None:
        """Flush the buffer if conditions are met or force is True."""
        self._cancel_idle()
        if self._buffer and (force or len(self._buffer) >= self._config.min_chars):
            await self._do_flush()

    def stop(self) -> None:
        """Cancel idle timer. Call when stream ends."""
        self._cancel_idle()

    def _find_sentence_break(self) -> int | None:
        """Find the position after the last sentence-ending punctuation."""
        last_match: re.Match[str] | None = None
        for match in _SENTENCE_END_RE.finditer(self._buffer):
            last_match = match
        if last_match is None:
            return None
        return last_match.end()

    async def _do_flush_up_to(self, pos: int) -> None:
        """Flush buffer content up to *pos*, keeping the rest."""
        if not self._buffer or self._flushing or pos <= 0:
            return
        self._flushing = True
        try:
            text = self._buffer[:pos]
            self._buffer = self._buffer[pos:]
            logger.debug("Coalescer flush chars=%d reason=%s", len(text), "boundary")
            await self._on_flush(text)
        finally:
            self._flushing = False

    async def _do_flush(self) -> None:
        """Send buffered text to the callback and clear the buffer."""
        if not self._buffer or self._flushing:
            return
        self._flushing = True
        try:
            text = self._buffer
            self._buffer = ""
            logger.debug("Coalescer flush chars=%d reason=%s", len(text), "full")
            await self._on_flush(text)
        finally:
            self._flushing = False

    def _start_idle(self) -> None:
        """Start the idle timer that flushes after idle_ms."""
        self._cancel_idle()
        loop = self._get_loop()
        delay = self._config.idle_ms / 1000.0
        self._idle_handle = loop.call_later(delay, self._idle_fired)

    def _idle_fired(self) -> None:
        """Called when idle timer expires -- schedule async flush."""
        self._idle_handle = None
        loop = self._get_loop()
        task = loop.create_task(self._do_flush())
        task.add_done_callback(self._flush_task_done)

    @staticmethod
    def _flush_task_done(task: asyncio.Task[None]) -> None:
        """Log exceptions from idle-triggered flush tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Idle flush failed: %s", exc, exc_info=exc)

    def _cancel_idle(self) -> None:
        """Cancel any pending idle timer."""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Cache and return the running event loop."""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop
