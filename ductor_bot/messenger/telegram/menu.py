"""The inline menu, and the one-button panel that opens it.

Telegram's toggle beside the input box is bound to reply keyboards and cannot
be pointed at anything else. So the panel holds a single button, ``/menu``,
and everything after that is an inline keyboard on a message.

That split is the point. Exactly one command is ever sent as text — and it is
deleted on arrival — while the menu itself is callbacks, which never enter the
message stream. Nothing here can be mistaken for something to send to the
agent, and nothing has to be intercepted to keep it out.

Items are fixed rather than filtered by state. A button that appears and
disappears reads as a broken screen, and a stable grid is easier to aim at on a
phone; where state matters it is shown in the header instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from ductor_bot.i18n import t
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from collections.abc import Sequence

MNU_PREFIX = "mnu:"
#: Closing removes the message rather than editing it: a menu left lying in the
#: topic is the clutter this is meant to avoid.
MNU_CLOSE = "mnu:x"

_PER_ROW = 2


@dataclass(frozen=True, slots=True)
class MenuItem:
    """One entry: a translated label, and the command it stands for."""

    key: str
    command: str


#: Order is reach-for order, not alphabetical.
MENU_ITEMS: tuple[MenuItem, ...] = (
    MenuItem("files", "/files"),
    MenuItem("folder", "/folder"),
    MenuItem("persona", "/persona"),
    MenuItem("model", "/model"),
    MenuItem("account", "/account"),
    MenuItem("skills", "/skills"),
    MenuItem("compact", "/compact"),
    MenuItem("clear", "/clear"),
    MenuItem("handoff", "/handoff"),
    MenuItem("status", "/status"),
    MenuItem("consult", "/consult"),
    MenuItem("help", "/help"),
)


def is_menu_callback(data: str) -> bool:
    return data.startswith(MNU_PREFIX)


def parse_callback(data: str) -> int | None:
    """Extract the item index from ``mnu:<index>``. None for close or junk."""
    raw = data[len(MNU_PREFIX) :]
    try:
        return int(raw)
    except ValueError:
        return None


def build_menu(subtitle: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """The menu message. *subtitle* carries current state, if any."""
    buttons = [
        InlineKeyboardButton(text=t(f"menu.item_{item.key}"), callback_data=f"{MNU_PREFIX}{i}")
        for i, item in enumerate(MENU_ITEMS)
    ]
    rows = [buttons[i : i + _PER_ROW] for i in range(0, len(buttons), _PER_ROW)]
    rows.append([InlineKeyboardButton(text=t("menu.btn_close"), callback_data=MNU_CLOSE)])

    body = [t("menu.header")]
    if subtitle:
        body += ["", subtitle]
    return fmt("\n".join(body), SEP, t("menu.hint")), InlineKeyboardMarkup(inline_keyboard=rows)


def state_subtitle(folder: str, persona: str, model: str) -> str:
    """Current bindings, shown rather than used to hide buttons."""
    parts = [
        f"📁 {folder}" if folder else "",
        f"👤 {persona}" if persona else "",
        f"🧠 {model}" if model else "",
    ]
    return "  ·  ".join(p for p in parts if p)


def build_toggle_panel(commands: Sequence[str] = ("/menu",)) -> ReplyKeyboardMarkup:
    """The one-button panel behind Telegram's own toggle.

    A single button on purpose: it is the only text this feature ever sends, so
    it is the only thing that could ever be mistaken for a message.
    """
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=c) for c in commands]],
        resize_keyboard=True,
        is_persistent=True,
    )


def remove_toggle_panel() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()
