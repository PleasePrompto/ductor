"""A persistent keyboard of the commands used most often.

Telegram gives every chat with a ``ReplyKeyboardMarkup`` a toggle beside the
input box, so showing and hiding the panel costs nothing to implement — the
client does it. What the bot decides is whether a keyboard exists at all.

The buttons carry command text because that is the only thing a reply keyboard
can do: whatever is written on the button is what gets sent. A prettier label
would have to be matched back to a command by string, which breaks in every
language and would swallow anyone who typed the same words.
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

#: Two per row, in the order they are reached for. Deliberately short: a
#: keyboard that fills the screen is worse than the menu it replaces.
MENU_ROWS: tuple[tuple[str, ...], ...] = (
    ("files", "folder"),
    ("persona", "account"),
    ("model", "new"),
    ("status", "help"),
)


def build_menu_keyboard(bot_username: str | None, *, mention: bool) -> ReplyKeyboardMarkup:
    """The command panel.

    *mention* appends ``@botname`` to every command. In a group configured with
    ``group_mention_only``, a bare ``/files`` is not treated as addressed to
    this bot and would be ignored — the keyboard would look fine and do
    nothing.
    """
    suffix = f"@{bot_username}" if mention and bot_username else ""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=f"/{name}{suffix}") for name in row] for row in MENU_ROWS
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=None,
    )


def remove_menu_keyboard() -> ReplyKeyboardRemove:
    """Take the panel away, restoring the plain input box."""
    return ReplyKeyboardRemove()


class MenuState:
    """Which chats are currently showing the panel.

    Memory only. Telegram offers no way to ask whether a keyboard is present,
    so this is the bot's own record; after a restart ``/menu`` shows the panel
    again, which is the harmless direction to be wrong in.
    """

    def __init__(self) -> None:
        self._shown: set[int] = set()

    def toggle(self, chat_id: int) -> bool:
        """Flip the panel for *chat_id*. Returns True when it is now shown."""
        if chat_id in self._shown:
            self._shown.discard(chat_id)
            return False
        self._shown.add(chat_id)
        return True

    def is_shown(self, chat_id: int) -> bool:
        return chat_id in self._shown
