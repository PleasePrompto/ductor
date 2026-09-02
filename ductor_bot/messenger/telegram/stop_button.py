"""The Stop control on a running turn — Ctrl+C, as a button.

Work that used to be handed to a background task now runs in the
conversation's own session, so a turn can last minutes or hours. The only
thing missing from the terminal experience was a way to say "no, not like
that" without waiting for it to finish.

Stop sends SIGINT, which the CLI handles itself: it records the interruption
in the transcript, closes the outstanding tool call, and leaves the session
resumable — verified against the real binary before this existed. Whatever is
queued behind the turn then runs immediately, which is what Ctrl+C does in a
terminal.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.i18n import t

#: Scoped to the topic it was pressed in, never the whole group: one topic is
#: one session, and stopping a website deploy must not stop an unrelated run.
STOP_TURN = "stp:x"


def stop_markup() -> InlineKeyboardMarkup:
    """The keyboard carried by a message while its turn is still running."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("turn.btn_stop"), callback_data=STOP_TURN)]]
    )


def is_stop_callback(data: str) -> bool:
    return data == STOP_TURN
