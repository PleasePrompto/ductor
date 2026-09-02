"""Stop is Ctrl+C: it belongs to the running turn, and only to its own topic."""

from __future__ import annotations

import asyncio

import pytest

from ductor_bot.i18n import init
from ductor_bot.messenger.telegram.edit_streaming import EditStreamEditor
from ductor_bot.messenger.telegram.stop_button import STOP_TURN, is_stop_callback, stop_markup

LOCALES = ("de", "en", "es", "fr", "id", "nl", "pt", "ru")


@pytest.fixture(autouse=True)
def _english() -> None:
    init("en")


def test_the_button_carries_the_stop_callback() -> None:
    row = stop_markup().inline_keyboard[0]

    assert len(row) == 1
    assert row[0].callback_data == STOP_TURN
    assert is_stop_callback(STOP_TURN) is True
    assert is_stop_callback("mnu:x") is False


@pytest.mark.parametrize("locale", LOCALES)
def test_the_label_is_translated_and_fits(locale: str) -> None:
    init(locale)
    try:
        label = stop_markup().inline_keyboard[0][0].text
        assert label.strip()
        assert len(label) <= 22, f"{locale}: {label!r}"
    finally:
        init("en")


class _Msg:
    def __init__(self, message_id: int = 1) -> None:
        self.message_id = message_id


class _Bot:
    """Records what a live turn would send and edit."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.markup_edits: list[object] = []

    async def send_message(self, **kwargs) -> _Msg:
        self.sent.append(kwargs)
        return _Msg(len(self.sent))

    async def edit_message_text(self, **_kwargs) -> _Msg:
        return _Msg(1)

    async def edit_message_reply_markup(self, **kwargs) -> None:
        self.markup_edits.append(kwargs.get("reply_markup"))


def test_a_running_turn_offers_stop() -> None:
    """Without this the only way to stop a long turn is to wait it out."""
    bot = _Bot()
    editor = EditStreamEditor(bot, chat_id=-100, live_markup=stop_markup())  # type: ignore[arg-type]

    asyncio.run(editor.append_text("working on it"))

    assert bot.sent, "no message was sent"
    assert bot.sent[0]["reply_markup"] is not None


def test_a_finished_turn_no_longer_offers_stop() -> None:
    """A Stop button on a finished answer would stop the *next* turn."""
    bot = _Bot()
    editor = EditStreamEditor(bot, chat_id=-100, live_markup=stop_markup())  # type: ignore[arg-type]

    async def scenario() -> None:
        await editor.append_text("working on it")
        await editor.finalize("done")

    asyncio.run(scenario())

    assert None in bot.markup_edits, "the live keyboard was never cleared"


def test_no_keyboard_is_attached_when_none_was_asked_for() -> None:
    bot = _Bot()
    editor = EditStreamEditor(bot, chat_id=-100)  # type: ignore[arg-type]

    asyncio.run(editor.append_text("plain turn"))

    assert bot.sent[0]["reply_markup"] is None
