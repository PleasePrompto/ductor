"""Announcing a persona change to the turn that first runs under it.

Changing persona mid-conversation does swap the system prompt and the tool set —
that part was never broken. What it does not do is tell the model, which goes on
answering in the shape the previous turns established because that is the only
evidence of identity inside the transcript. These tests pin *when* the
announcement is produced, and just as importantly when it is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ductor_bot.personas.store import NO_PERSONA, PersonaStore


@pytest.fixture
def store(tmp_path: Path) -> PersonaStore:
    return PersonaStore(tmp_path / "personas.json")


KEY = "tg:-100123:110"


def test_a_real_change_is_announced_once(store: PersonaStore) -> None:
    store.set(KEY, "coder")
    store.set(KEY, "designer")

    assert store.take_switch(KEY) == ("coder", "designer")
    # Once. A second turn must not be told again.
    assert store.take_switch(KEY) is None


def test_first_choice_is_not_a_switch(store: PersonaStore) -> None:
    """Answering the gate on a new conversation has no history to contradict."""
    store.set(KEY, "coder")

    assert store.take_switch(KEY) is None


def test_choosing_the_same_persona_again_is_not_a_switch(store: PersonaStore) -> None:
    store.set(KEY, "coder")
    store.set(KEY, "coder")

    assert store.take_switch(KEY) is None


def test_switching_to_or_from_no_persona_is_not_announced(store: PersonaStore) -> None:
    """"No persona" is the absence of a remit, so there is nothing to contrast."""
    store.set(KEY, "coder")
    store.set(KEY, NO_PERSONA)
    assert store.take_switch(KEY) is None

    store.set(KEY, "designer")
    assert store.take_switch(KEY) is None


def test_the_announcement_is_per_conversation(store: PersonaStore) -> None:
    other = "tg:-100123:97"
    store.set(KEY, "coder")
    store.set(other, "coder")
    store.set(KEY, "designer")

    assert store.take_switch(other) is None
    assert store.take_switch(KEY) == ("coder", "designer")


def test_clear_drops_a_pending_announcement(store: PersonaStore) -> None:
    """/new starts a conversation with no history, so there is nothing to explain."""
    store.set(KEY, "coder")
    store.set(KEY, "designer")
    store.clear(KEY)

    assert store.take_switch(KEY) is None


def test_the_choice_itself_still_persists(store: PersonaStore, tmp_path: Path) -> None:
    store.set(KEY, "coder")
    store.set(KEY, "designer")

    reloaded = PersonaStore(tmp_path / "personas.json")
    assert reloaded.get(KEY) == "designer"
    # The announcement is memory-only: it would arrive with no conversation
    # around it after a restart.
    assert reloaded.take_switch(KEY) is None
