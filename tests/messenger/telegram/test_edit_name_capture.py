"""Collecting a typed name, and tidying up after it.

The name is an answer to a question, not a message in the conversation. Left in
place it makes the exchange read backwards — the menu updates above while the
input sits at the bottom — and folder names pile up in the topic.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from ductor_bot.files.edits import EditStore
from ductor_bot.i18n import init
from ductor_bot.messenger.telegram.app import TelegramBot
from ductor_bot.session.key import SessionKey

KEY = SessionKey(transport="tg", chat_id=-100123, topic_id=7)


class _Bot:
    def __init__(self, *, can_delete: bool = True) -> None:
        self.can_delete = can_delete
        self.deleted: list[int] = []
        self.edited: list[int] = []
        self.sent: list[str] = []

    async def delete_message(self, chat_id: int, message_id: int):
        if not self.can_delete:
            raise TelegramBadRequest(
                method=SimpleNamespace(), message="Bad Request: message can't be deleted"
            )
        self.deleted.append(message_id)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs["message_id"])

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(message_id=999)


@pytest.fixture(autouse=True)
def _english() -> None:
    init("en")


@pytest.fixture
def app(tmp_path: Path) -> TelegramBot:
    proj = tmp_path / "EMR"
    proj.mkdir()
    (proj / "notes.md").write_text("x")

    instance = object.__new__(TelegramBot)
    instance._bot = _Bot()
    instance._config = SimpleNamespace(project_roots={"EMR": str(proj)})
    instance._orchestrator = SimpleNamespace(
        paths=SimpleNamespace(ductor_home=tmp_path / ".ductor", workspace=tmp_path / "ws")
    )
    (tmp_path / ".ductor").mkdir()
    instance._edit_store = EditStore()
    instance._proj = proj
    return instance


def _message(text: str):
    return SimpleNamespace(
        text=text,
        message_id=42,
        chat=SimpleNamespace(id=KEY.chat_id),
        message_thread_id=KEY.topic_id,
        # get_thread_id() consults this before the thread id; a forum message
        # without it is not a shape Telegram ever sends.
        is_topic_message=True,
    )


async def test_the_typed_name_is_removed_from_the_chat(app) -> None:
    app._edit_store.begin(KEY.storage_key, "rename", app._proj / "notes.md")

    handled = await app._collect_edit_name(_message("renamed.md"), KEY)

    assert handled is True
    assert app._bot.deleted == [42], "the answer should not stay in the topic"
    assert app._edit_store.get(KEY.storage_key).name == "renamed.md"


async def test_the_flow_survives_missing_delete_rights(app) -> None:
    """Deletion needs a group setting the bot does not control."""
    app._bot = _Bot(can_delete=False)
    app._edit_store.begin(KEY.storage_key, "rename", app._proj / "notes.md")

    handled = await app._collect_edit_name(_message("renamed.md"), KEY)

    assert handled is True, "a failed cleanup must not lose the name"
    assert app._edit_store.get(KEY.storage_key).name == "renamed.md"


async def test_a_message_with_no_pending_edit_is_left_alone(app) -> None:
    """Ordinary conversation must never be consumed or deleted."""
    handled = await app._collect_edit_name(_message("hello there"), KEY)

    assert handled is False
    assert app._bot.deleted == []
