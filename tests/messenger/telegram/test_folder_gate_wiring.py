"""The real gate methods, not a re-implementation of them.

``test_folder_gate.py`` checks the ordering rules against a model of the app.
This file drives ``TelegramBot``'s actual methods, so a change to the real
wiring cannot pass while the model still agrees with itself.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from ductor_bot.i18n import init
from ductor_bot.messenger.telegram.app import TelegramBot
from ductor_bot.session.key import SessionKey
from ductor_bot.workspace.topic_bindings import SHARED_WORKSPACE, BindingStore

KEY = SessionKey(transport="tg", chat_id=-100123, topic_id=7)


class _Bot:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edited: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs.get("text", ""))


@pytest.fixture(autouse=True)
def _english() -> None:
    init("en")


@pytest.fixture
def app(tmp_path: Path) -> TelegramBot:
    """A TelegramBot with only the attributes these methods touch.

    __init__ builds an aiogram Dispatcher and a bus; none of that is involved
    in gate ordering, and constructing it would test aiogram instead.
    """
    emr = tmp_path / "EMR"
    emr.mkdir()
    config = SimpleNamespace(
        project_roots={"EMR": str(emr)},
        persona_prompt=True,
        streaming=SimpleNamespace(enabled=False),
        # Real AgentConfig always carries this; the gate now consults it to
        # decide whether a topic is the managed Consult one.
        managed_topics=False,
    )
    instance = object.__new__(TelegramBot)
    instance._bot = _Bot()
    instance._config = config
    # The selector reads roots off the orchestrator, the gate off the app's own
    # config; both point at the same object so they cannot drift in the test.
    instance._orchestrator = SimpleNamespace(
        config=config,
        paths=SimpleNamespace(
            ductor_home=tmp_path,
            consult_dir=tmp_path / "Consult",
            managed_topics_path=tmp_path / "managed_topics.json",
        ),
        bindings=BindingStore(tmp_path / "bindings.json"),
        personas=SimpleNamespace(
            has_choice=lambda _k: False,
            hold=lambda _k, _t: None,
        ),
    )
    return instance


async def test_unbound_conversation_is_asked_and_the_message_is_held(app) -> None:
    held = await app._ask_folder_if_needed(KEY, "fix the bug")

    assert held is True, "the caller must not process the message"
    assert app._orchestrator.bindings.take(KEY.storage_key) == "fix the bug"
    assert "no folder yet" in app._bot.sent[0].lower()


async def test_a_bound_conversation_is_not_asked(app) -> None:
    app._orchestrator.bindings.set(KEY.storage_key, SHARED_WORKSPACE)
    assert await app._ask_folder_if_needed(KEY, "hello") is False
    assert app._bot.sent == []


async def test_no_configured_roots_means_no_gate(app) -> None:
    """An installation that configured nothing must not be locked out."""
    app._config.project_roots = {}
    assert await app._ask_folder_if_needed(KEY, "hello") is False


async def test_choosing_a_folder_hands_the_message_to_the_persona_gate(app, monkeypatch) -> None:
    """The bug this guards: running the held message straight away would start
    work with no persona chosen."""
    await app._ask_folder_if_needed(KEY, "fix the bug")

    handed: list[str] = []

    async def fake_persona_gate(_key, text, **_kw):
        handed.append(text)
        return True  # persona gate holds it in turn

    monkeypatch.setattr(app, "_ask_persona_if_needed", fake_persona_gate)
    await app._handle_folder_selector(KEY, message_id=1, data="fld:0")

    assert handed == ["fix the bug"], "the held message must reach the persona gate"
    assert app._orchestrator.bindings.get(KEY.storage_key) is not None


async def test_the_message_runs_once_both_gates_are_satisfied(app, monkeypatch) -> None:
    await app._ask_folder_if_needed(KEY, "fix the bug")

    ran: list[str] = []

    async def no_persona_needed(_key, _text, **_kw):
        return False

    async def capture(_reply_to, _key, text, **_kw):
        ran.append(text)

    monkeypatch.setattr(app, "_ask_persona_if_needed", no_persona_needed)
    monkeypatch.setattr(app, "_handle_non_streaming", capture)
    monkeypatch.setattr(
        app,
        "_sequential",
        SimpleNamespace(get_lock=lambda _k: contextlib.nullcontext()),
        raising=False,
    )

    await app._handle_folder_selector(KEY, message_id=1, data="fld:0")
    assert ran == ["fix the bug"]


async def test_shared_workspace_is_a_real_answer(app, monkeypatch) -> None:
    """Picking it must satisfy the gate, not leave the conversation unbound."""
    await app._ask_folder_if_needed(KEY, "hello")

    async def no_persona_needed(_key, _text, **_kw):
        return False

    monkeypatch.setattr(app, "_ask_persona_if_needed", no_persona_needed)
    monkeypatch.setattr(app, "_handle_non_streaming", _ignore)
    monkeypatch.setattr(
        app,
        "_sequential",
        SimpleNamespace(get_lock=lambda _k: contextlib.nullcontext()),
        raising=False,
    )

    await app._handle_folder_selector(KEY, message_id=1, data="fld:-1")

    assert app._orchestrator.bindings.has_choice(KEY.storage_key) is True
    assert app._orchestrator.bindings.resolve(KEY.storage_key) is None
    assert await app._ask_folder_if_needed(KEY, "again") is False


async def _ignore(*_args: object, **_kwargs: object) -> None:
    """Stand-in for the dispatch path; this test is about the binding."""
    return
