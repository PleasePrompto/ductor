"""Edit-mode stream editor for Telegram messages.

Maintains a single Telegram message that is continuously edited as content
arrives.  Consecutive identical tool events are collapsed (``[TOOL: Bash] x3``).
Falls back to append mode after persistent edit failures.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from ductor_bot.bot.buttons import extract_buttons
from ductor_bot.bot.formatting import (
    TELEGRAM_MSG_LIMIT,
    markdown_to_telegram_html,
    split_html_message,
)

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ToolEntry:
    """A single indicator with a repeat count and display style."""

    name: str
    count: int = 1
    style: str = "tool"


class _ToolTracker:
    """Collapse consecutive identical indicators into counted entries.

    Handles both tool indicators (``[TOOL: Bash]``) and system indicators
    (``[THINKING]``).  Entries are only collapsed when *name* and *style*
    match the previous entry.
    """

    def __init__(self) -> None:
        self._entries: list[_ToolEntry] = []

    def add(self, name: str, *, style: str = "tool") -> None:
        """Record an indicator, incrementing count if same as previous."""
        if self._entries and self._entries[-1].name == name and self._entries[-1].style == style:
            self._entries[-1].count += 1
        else:
            self._entries.append(_ToolEntry(name=name, style=style))

    def render_html(self) -> str:
        """Render all entries as Telegram HTML lines."""
        parts: list[str] = []
        for entry in self._entries:
            escaped = html.escape(entry.name)
            suffix = f" x{entry.count}" if entry.count > 1 else ""
            if entry.style == "system":
                parts.append(f"<i>[{escaped}]{suffix}</i>")
            else:
                parts.append(f"<b>[TOOL: {escaped}]{suffix}</b>")
        return "\n".join(parts)

    @property
    def has_entries(self) -> bool:
        return bool(self._entries)


@dataclass(slots=True)
class _EditorState:
    """Mutable state for the edit-mode stream editor."""

    segments: list[str] = field(default_factory=list)
    indicator_indices: set[int] = field(default_factory=set)
    raw_text_parts: list[str] = field(default_factory=list)
    tool_tracker: _ToolTracker = field(default_factory=_ToolTracker)
    active_msg: Message | None = None
    sealed_segment_idx: int = 0
    messages_sent: int = 0
    last_edit_time: float = 0.0
    edit_timer: asyncio.TimerHandle | None = None
    consecutive_failures: int = 0
    fallen_back: bool = False


class EditStreamEditor:
    """Single-message editor: content is accumulated and the message is edited in-place.

    When content exceeds Telegram's 4096-char limit the current message is
    sealed and a new one is started.  After ``max_edit_failures`` consecutive
    edit errors the editor degrades to append mode.
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        reply_to: Message | None = None,
        edit_interval_seconds: float = 2.0,
        max_edit_failures: int = 3,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._reply_to = reply_to
        self._interval = edit_interval_seconds
        self._max_failures = max_edit_failures
        self._s = _EditorState()

    @property
    def has_content(self) -> bool:
        """True if at least one message has been sent."""
        return self._s.messages_sent > 0

    async def append_text(self, text: str) -> None:
        """Accumulate a text chunk and schedule an edit."""
        if not text.strip():
            return
        if self._s.fallen_back:
            await self._send_new(markdown_to_telegram_html(text))
            return
        # Transition tool -> text: seal the tool block
        self._flush_tool_segment()
        self._s.raw_text_parts.append(text)
        await self._schedule_edit()

    async def append_tool(self, tool_name: str) -> None:
        """Record a tool event (collapsed with consecutive duplicates)."""
        if self._s.fallen_back:
            indicator = f"<b>[TOOL: {html.escape(tool_name)}]</b>"
            await self._send_new(indicator)
            return
        # Transition text -> tool: seal the text block
        self._flush_text_segment()
        self._s.tool_tracker.add(tool_name)
        await self._schedule_edit()

    async def append_system(self, text: str) -> None:
        """Show a system status indicator (e.g. THINKING, COMPACTING).

        Routed through the tool tracker so consecutive identical entries
        are collapsed (``[THINKING] x3``).
        """
        if self._s.fallen_back:
            await self._send_new(f"<i>[{html.escape(text)}]</i>")
            return
        self._flush_text_segment()
        self._s.tool_tracker.add(text, style="system")
        await self._schedule_edit()

    async def finalize(self, full_text: str) -> None:
        """Force a final edit with indicators stripped for a clean message."""
        self._cancel_timer()
        if self._s.fallen_back:
            return
        self._flush_text_segment()
        # Discard pending indicators and strip flushed ones from active portion.
        self._s.tool_tracker = _ToolTracker()
        self._strip_active_indicators()
        await self._do_edit()
        await self._attach_buttons(full_text)

    # ------------------------------------------------------------------
    # Internal: segment management
    # ------------------------------------------------------------------

    def _flush_text_segment(self) -> None:
        """Convert pending raw text into an HTML segment."""
        if not self._s.raw_text_parts:
            return
        raw = "".join(self._s.raw_text_parts)
        self._s.raw_text_parts = []
        if raw.strip():
            self._s.segments.append(markdown_to_telegram_html(raw))

    def _flush_tool_segment(self) -> None:
        """Convert the tool tracker into an HTML segment (marked as indicator)."""
        if not self._s.tool_tracker.has_entries:
            return
        self._s.indicator_indices.add(len(self._s.segments))
        self._s.segments.append(self._s.tool_tracker.render_html())
        self._s.tool_tracker = _ToolTracker()

    def _strip_active_indicators(self) -> None:
        """Remove indicator segments from the active (un-sealed) portion."""
        start = self._s.sealed_segment_idx
        cleaned = [
            self._s.segments[i]
            for i in range(start, len(self._s.segments))
            if i not in self._s.indicator_indices
        ]
        self._s.segments = self._s.segments[:start] + cleaned
        self._s.indicator_indices.clear()

    def _render_active_html(self) -> str:
        """Render un-sealed segments + pending text/tools (read-only)."""
        parts = list(self._s.segments[self._s.sealed_segment_idx :])
        if self._s.raw_text_parts:
            raw = "".join(self._s.raw_text_parts)
            if raw.strip():
                parts.append(markdown_to_telegram_html(raw))
        if self._s.tool_tracker.has_entries:
            parts.append(self._s.tool_tracker.render_html())
        return "\n\n".join(seg for seg in parts if seg.strip())

    # ------------------------------------------------------------------
    # Internal: edit scheduling / throttling
    # ------------------------------------------------------------------

    async def _schedule_edit(self) -> None:
        """Edit immediately if interval has passed, otherwise defer."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._s.last_edit_time
        if elapsed >= self._interval:
            await self._do_edit()
        elif self._s.edit_timer is None:
            delay = self._interval - elapsed
            loop = asyncio.get_running_loop()
            self._s.edit_timer = loop.call_later(delay, self._deferred_edit_fired)

    def _deferred_edit_fired(self) -> None:
        """Timer callback -- schedule the async edit on the event loop."""
        self._s.edit_timer = None
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._do_edit())
        task.add_done_callback(_log_task_error)

    def _cancel_timer(self) -> None:
        if self._s.edit_timer is not None:
            self._s.edit_timer.cancel()
            self._s.edit_timer = None

    # ------------------------------------------------------------------
    # Internal: message creation / editing
    # ------------------------------------------------------------------

    async def _do_edit(self) -> None:
        """Render content and create or edit the Telegram message."""
        full_html = self._render_active_html()
        if not full_html.strip():
            return

        chunks = split_html_message(full_html, max_len=TELEGRAM_MSG_LIMIT)

        if len(chunks) > 1:
            await self._handle_overflow(chunks)
            return

        if self._s.active_msg is None:
            await self._create_message(chunks[0])
        else:
            await self._edit_message(chunks[0])

        self._s.last_edit_time = asyncio.get_event_loop().time()

    async def _handle_overflow(self, chunks: list[str]) -> None:
        """Seal current message with first chunk, continue in a new one."""
        if self._s.active_msg is not None:
            await self._edit_message(chunks[0])
        else:
            await self._create_message(chunks[0])

        # Seal: reset state for continuation in a new message
        logger.debug("Message sealed, starting new segment")
        self._s.active_msg = None
        self._s.sealed_segment_idx = len(self._s.segments)

        remaining = "\n\n".join(chunks[1:])
        if remaining.strip():
            await self._create_message(remaining)
        self._s.last_edit_time = asyncio.get_event_loop().time()

    async def _create_message(self, text: str) -> None:
        """Send a new message (reply for the first one)."""
        display = text[:TELEGRAM_MSG_LIMIT]
        if not display.strip():
            return
        try:
            if self._s.messages_sent == 0 and self._reply_to is not None:
                msg = await self._reply_to.answer(display, parse_mode=ParseMode.HTML)
            else:
                msg = await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=display,
                    parse_mode=ParseMode.HTML,
                )
            self._s.active_msg = msg
            self._s.messages_sent += 1
            logger.debug("Message created msg_id=%d", msg.message_id)
        except TelegramBadRequest:
            logger.warning("HTML create failed, falling back to plain text")
            await self._create_message_plain(display)

    async def _create_message_plain(self, text: str) -> None:
        """Fallback: send without HTML parse mode."""
        try:
            msg = await self._bot.send_message(
                chat_id=self._chat_id,
                text=text[:TELEGRAM_MSG_LIMIT],
                parse_mode=None,
            )
            self._s.active_msg = msg
            self._s.messages_sent += 1
        except TelegramBadRequest:
            logger.exception("Failed to send even as plain text")

    async def _edit_message(self, text: str) -> None:
        """Edit the active Telegram message with error handling."""
        if self._s.active_msg is None:
            return
        display = text[:TELEGRAM_MSG_LIMIT]
        try:
            await self._bot.edit_message_text(
                text=display,
                chat_id=self._chat_id,
                message_id=self._s.active_msg.message_id,
                parse_mode=ParseMode.HTML,
            )
            self._s.consecutive_failures = 0
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            self._s.consecutive_failures += 1
            logger.warning(
                "Edit failed (%d/%d): %s",
                self._s.consecutive_failures,
                self._max_failures,
                exc,
            )
            if self._s.consecutive_failures >= self._max_failures:
                logger.warning("Too many edit failures, falling back to append mode")
                self._s.fallen_back = True
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            try:
                await self._bot.edit_message_text(
                    text=display,
                    chat_id=self._chat_id,
                    message_id=self._s.active_msg.message_id,
                    parse_mode=ParseMode.HTML,
                )
                self._s.consecutive_failures = 0
            except (TelegramBadRequest, TelegramRetryAfter):
                logger.warning("Edit retry after rate-limit also failed")

    # ------------------------------------------------------------------
    # Internal: button keyboard attachment
    # ------------------------------------------------------------------

    async def _attach_buttons(self, full_text: str) -> None:
        """Parse buttons from *full_text* and attach keyboard to the active message."""
        if self._s.active_msg is None:
            return
        _, markup = extract_buttons(full_text)
        if markup is None:
            return
        try:
            await self._bot.edit_message_reply_markup(
                chat_id=self._chat_id,
                message_id=self._s.active_msg.message_id,
                reply_markup=markup,
            )
        except (TelegramBadRequest, TelegramRetryAfter):
            logger.warning("Failed to attach button keyboard")

    # ------------------------------------------------------------------
    # Internal: append-mode fallback
    # ------------------------------------------------------------------

    async def _send_new(self, formatted: str) -> None:
        """Fallback: send formatted content as new messages (append mode)."""
        for chunk in split_html_message(formatted):
            display = chunk[:TELEGRAM_MSG_LIMIT]
            if not display.strip():
                continue
            try:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=display,
                    parse_mode=ParseMode.HTML,
                )
            except TelegramBadRequest:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=display,
                    parse_mode=None,
                )
            self._s.messages_sent += 1


def _log_task_error(task: asyncio.Task[None]) -> None:
    """Log exceptions from deferred edit tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Deferred edit failed: %s", exc, exc_info=exc)
