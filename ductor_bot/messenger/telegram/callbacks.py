"""Callback helpers for inline keyboard handling in the Telegram bot.

Extracts reusable patterns from the TelegramBot callback routing so the
four selector handlers (model, cron, session, task) share a single
implementation.
"""

from __future__ import annotations

import contextlib
import html as html_mod
import re
from typing import TYPE_CHECKING

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

from ductor_bot.messenger.telegram.formatting import markdown_to_telegram_html
from ductor_bot.orchestrator.selectors.models import ButtonGrid, SelectorResponse

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup, Message


_USER_ANSWER_RE = re.compile(r"\n+\[USER ANSWER\].*$", re.DOTALL)
_CONTEXT_QUESTION_RE = re.compile(r"([^\n?]{1,240}\?)")
_BUTTON_PROMPT_LIMIT = 1600


# ---------------------------------------------------------------------------
# ButtonGrid -> InlineKeyboardMarkup conversion
# ---------------------------------------------------------------------------


def button_grid_to_markup(grid: ButtonGrid | None) -> InlineKeyboardMarkup | None:
    """Convert abstract ``ButtonGrid`` to aiogram ``InlineKeyboardMarkup``."""
    if grid is None:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=btn.text, callback_data=btn.callback_data) for btn in row]
            for row in grid.rows
        ]
    )


# ---------------------------------------------------------------------------
# Selector result editing (shared by model / cron / session / task wizards)
# ---------------------------------------------------------------------------


async def edit_selector_response(
    bot: Bot,
    chat_id: int,
    message_id: int,
    resp: SelectorResponse,
) -> None:
    """Edit a message in-place with a ``SelectorResponse``."""
    with contextlib.suppress(TelegramBadRequest):
        await bot.edit_message_text(
            text=markdown_to_telegram_html(resp.text),
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=button_grid_to_markup(resp.buttons),
            parse_mode=ParseMode.HTML,
        )


# ---------------------------------------------------------------------------
# Button choice annotation
# ---------------------------------------------------------------------------


async def mark_button_choice(bot: Bot, chat_id: int, msg: Message, label: str) -> None:
    """Edit the bot message to append ``[USER ANSWER] label`` and remove the keyboard.

    Falls back to keyboard-only removal when the message is a caption
    (photo/video) or the updated text would exceed Telegram limits.
    """
    if msg.text is not None:
        original_html = msg.html_text or msg.text
        escaped = html_mod.escape(label)
        updated = f"{original_html}\n\n<i>[USER ANSWER] {escaped}</i>"
        try:
            await bot.edit_message_text(
                text=updated,
                chat_id=chat_id,
                message_id=msg.message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
        else:
            return

    with contextlib.suppress(TelegramBadRequest):
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=msg.message_id,
            reply_markup=None,
        )


def _trim_button_context(text: str, *, limit: int = _BUTTON_PROMPT_LIMIT) -> str:
    """Return a bounded excerpt of the previous assistant message."""
    stripped = _USER_ANSWER_RE.sub("", text).strip()
    if len(stripped) <= limit:
        return stripped
    head = max(300, limit // 2)
    tail = max(300, limit - head - 5)
    return f"{stripped[:head].rstrip()}\n...\n{stripped[-tail:].lstrip()}"


def _extract_button_question(text: str) -> str | None:
    """Extract the last explicit question line from the prior assistant message."""
    stripped = _USER_ANSWER_RE.sub("", text).strip()
    if not stripped:
        return None
    for line in reversed([line.strip() for line in stripped.splitlines() if line.strip()]):
        if line.endswith("?"):
            return line
    matches = _CONTEXT_QUESTION_RE.findall(stripped)
    return matches[-1].strip() if matches else None


def build_button_followup_prompt(message_text: str | None, label: str) -> str:
    """Build a contextual follow-up prompt for a pressed inline button."""
    cleaned_label = label.strip()
    stripped = (message_text or "").strip()
    if not stripped:
        return f"Continue from the previous assistant message.\nUser selected this button: {cleaned_label}"

    excerpt = _trim_button_context(stripped)
    question = _extract_button_question(stripped)
    if question:
        return (
            "Continue from the previous assistant message.\n"
            f"Previous assistant question: {question}\n\n"
            f"Previous assistant message excerpt:\n{excerpt}\n\n"
            f"User selected this button: {cleaned_label}"
        )
    return (
        "Continue from the previous assistant message.\n"
        f"Previous assistant message excerpt:\n{excerpt}\n\n"
        f"User selected this button: {cleaned_label}"
    )


# ---------------------------------------------------------------------------
# Named-session callback helpers
# ---------------------------------------------------------------------------


def parse_ns_callback(data: str) -> tuple[str, str] | None:
    """Parse ``ns:<session_name>:<label>`` callback data.

    Returns ``(session_name, label)`` or ``None`` if the format is invalid.
    """
    rest = data[3:]  # strip "ns:"
    colon = rest.find(":")
    if colon < 0:
        return None
    session_name = rest[:colon]
    label = rest[colon + 1 :]
    if not session_name or not label:
        return None
    return session_name, label
