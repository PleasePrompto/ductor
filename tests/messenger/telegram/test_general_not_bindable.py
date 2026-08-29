"""A forum's General thread can never hold a folder binding.

General is the one thread whose messages carry no ``message_thread_id``, so it
collapses onto the same chat-level key a private chat uses. That is how a
message typed outside a topic once bound a project folder and started a fresh
conversation in it: the topic's own session, with all its history, was still on
disk and simply never consulted again.

Four separate paths can write a binding (the gate's picker, the picker
callback, /folder, and the file browser's "bind here"), so the rule is enforced
in the store rather than at each of them. These tests pin both halves: the
predicate that recognises General, and the store that refuses it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ductor_bot.messenger.telegram.topic import is_general_thread
from ductor_bot.workspace.topic_bindings import SHARED_WORKSPACE, BindingStore

GENERAL = "tg:-100123"
TOPIC = "tg:-100123:110"


def _message(*, is_forum: bool, is_topic_message: bool) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(is_forum=is_forum),
        is_topic_message=is_topic_message,
    )


def test_general_thread_of_a_forum_is_recognised() -> None:
    assert is_general_thread(_message(is_forum=True, is_topic_message=False))


def test_a_real_topic_is_not_general() -> None:
    assert not is_general_thread(_message(is_forum=True, is_topic_message=True))


def test_a_private_chat_is_not_general() -> None:
    # Its chat-level key is the only key it has, so binding must still work.
    assert not is_general_thread(_message(is_forum=False, is_topic_message=False))


def test_missing_message_is_not_general() -> None:
    assert not is_general_thread(None)


def test_protected_key_refuses_to_bind(tmp_path: Path) -> None:
    store = BindingStore(tmp_path / "bindings.json")
    store.protect(GENERAL)

    assert store.set(GENERAL, "/srv/wp-website") is False
    assert store.resolve(GENERAL) is None
    assert not (tmp_path / "bindings.json").exists()


def test_protected_key_never_asks(tmp_path: Path) -> None:
    """The gate reads has_choice; General must answer "already decided"."""
    store = BindingStore(tmp_path / "bindings.json")
    store.protect(GENERAL)

    assert store.has_choice(GENERAL)
    assert store.get(GENERAL) == SHARED_WORKSPACE


def test_a_stale_binding_is_neutralised(tmp_path: Path) -> None:
    """The regression itself: an entry written before this rule existed.

    Deleting it from disk is a migration; ignoring it at runtime is the part
    that has to hold even when nobody ran one.
    """
    project = tmp_path / "wp-website"
    project.mkdir()
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps({GENERAL: str(project)}), encoding="utf-8")

    store = BindingStore(path)
    assert store.resolve(GENERAL) == project  # before General is recognised

    store.protect(GENERAL)
    assert store.resolve(GENERAL) is None
    assert store.get(GENERAL) == SHARED_WORKSPACE


def test_topics_still_bind(tmp_path: Path) -> None:
    project = tmp_path / "wp-website"
    project.mkdir()
    store = BindingStore(tmp_path / "bindings.json")
    store.protect(GENERAL)

    assert store.set(TOPIC, str(project)) is True
    assert store.resolve(TOPIC) == project
