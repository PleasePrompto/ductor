"""Consult sees only its own directory, and menu items never reach the agent.

Two failures reported from the phone:

* the menu's Help button answered "that is not available in this environment" —
  it had been routed to the orchestrator registry, which does not know /help,
  so handle_message passed it to the agent as ordinary text;
* the Consult topic offered every project through the file manager and the
  folder picker, which is precisely what that topic exists not to do.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ductor_bot.i18n import init
from ductor_bot.messenger.telegram.managed_topics import CONSULT, ManagedTopicStore, TopicRecord
from ductor_bot.messenger.telegram.menu import MENU_ITEMS
from ductor_bot.session.key import SessionKey

CHAT = -100123
CONSULT_TOPIC = 93
PROJECT_TOPIC = 97


@pytest.fixture(autouse=True)
def _english() -> None:
    init("en")


@pytest.fixture
def app(tmp_path: Path):
    from ductor_bot.messenger.telegram.app import TelegramBot

    home = tmp_path / ".ductor"
    (home / "Consult").mkdir(parents=True)
    emr = tmp_path / "IT" / "EMR"
    emr.mkdir(parents=True)

    store = ManagedTopicStore(home / "managed_topics.json")
    store.set(CHAT, CONSULT, TopicRecord(topic_id=CONSULT_TOPIC, notice_message_id=94))

    instance = object.__new__(TelegramBot)
    instance._config = SimpleNamespace(
        project_roots={"EMR": str(emr)}, managed_topics=True
    )
    instance._orchestrator = SimpleNamespace(
        paths=SimpleNamespace(
            ductor_home=home,
            consult_dir=home / "Consult",
            managed_topics_path=home / "managed_topics.json",
        )
    )
    return instance


def _key(topic: int) -> SessionKey:
    return SessionKey(transport="tg", chat_id=CHAT, topic_id=topic)


def test_consult_is_recognised(app) -> None:
    assert app._is_consult(_key(CONSULT_TOPIC)) is True
    assert app._is_consult(_key(PROJECT_TOPIC)) is False
    assert app._is_consult(SessionKey(transport="tg", chat_id=CHAT)) is False


def test_consult_sees_only_its_own_directory(app) -> None:
    roots = app._roots_for(_key(CONSULT_TOPIC))
    assert list(roots) == ["Consult"]
    assert "EMR" not in roots


def test_other_topics_keep_every_project(app) -> None:
    assert "EMR" in app._roots_for(_key(PROJECT_TOPIC))


def test_scoping_is_off_when_managed_topics_are_off(app) -> None:
    """Nothing created the topic, so nothing should be treated as special."""
    app._config.managed_topics = False
    assert app._is_consult(_key(CONSULT_TOPIC)) is False
    assert "EMR" in app._roots_for(_key(CONSULT_TOPIC))


def test_every_menu_item_is_handled_without_the_agent() -> None:
    """A menu button must never become a message to Claude."""
    import inspect

    from ductor_bot.messenger.telegram.app import TelegramBot
    from ductor_bot.orchestrator import core

    registry = inspect.getsource(core)
    transport = inspect.getsource(TelegramBot._transport_menu_actions)

    for item in MENU_ITEMS:
        handled = (
            f'register_async("{item.command}"' in registry
            or f'"{item.command}"' in transport
        )
        assert handled, (
            f"{item.command} is neither an orchestrator command nor a transport "
            "action; handle_message would send it to the agent as plain text"
        )
