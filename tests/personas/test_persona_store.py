"""Tests for persona choices and held prompts."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.personas.store import NO_PERSONA, PersonaStore


def test_unanswered_conversation_has_no_choice(tmp_path: Path) -> None:
    store = PersonaStore(tmp_path / "personas.json")
    assert not store.has_choice("tg:1:2")
    assert store.get("tg:1:2") is None


def test_choice_survives_a_restart(tmp_path: Path) -> None:
    """A restart must not re-ask; the user already answered."""
    path = tmp_path / "personas.json"
    PersonaStore(path).set("tg:1:2", "coder")
    assert PersonaStore(path).get("tg:1:2") == "coder"


def test_explicit_none_is_distinct_from_unanswered(tmp_path: Path) -> None:
    """Choosing Default is an answer; it must not trigger the question again."""
    store = PersonaStore(tmp_path / "personas.json")
    store.set("tg:1:2", NO_PERSONA)
    assert store.has_choice("tg:1:2")
    assert store.get("tg:1:2") == NO_PERSONA


def test_clear_makes_the_next_message_ask_again(tmp_path: Path) -> None:
    """/new and /reset end the conversation, so the persona goes with it."""
    store = PersonaStore(tmp_path / "personas.json")
    store.set("tg:1:2", "coder")
    store.clear("tg:1:2")
    assert not store.has_choice("tg:1:2")


def test_choices_are_isolated_per_conversation(tmp_path: Path) -> None:
    store = PersonaStore(tmp_path / "personas.json")
    store.set("tg:1:2", "coder")
    store.set("tg:1:3", "scout")
    assert store.get("tg:1:2") == "coder"
    assert store.get("tg:1:3") == "scout"


def test_held_prompt_round_trips_once(tmp_path: Path) -> None:
    store = PersonaStore(tmp_path / "personas.json")
    store.hold("tg:1:2", "fix the failing test")
    assert store.peek("tg:1:2") == "fix the failing test"
    assert store.take("tg:1:2") == "fix the failing test"
    assert store.take("tg:1:2") is None


def test_held_prompts_are_not_persisted(tmp_path: Path) -> None:
    """A queued instruction must not survive a restart and run unwatched."""
    path = tmp_path / "personas.json"
    store = PersonaStore(path)
    store.hold("tg:1:2", "delete everything")
    assert PersonaStore(path).take("tg:1:2") is None


def test_clear_drops_a_held_prompt(tmp_path: Path) -> None:
    store = PersonaStore(tmp_path / "personas.json")
    store.hold("tg:1:2", "old work")
    store.clear("tg:1:2")
    assert store.take("tg:1:2") is None


def test_corrupt_store_is_survivable(tmp_path: Path) -> None:
    path = tmp_path / "personas.json"
    path.write_text("{ not json")
    assert PersonaStore(path).get("tg:1:2") is None


def test_unreadable_store_degrades_to_asking_again(tmp_path: Path) -> None:
    """Anything unreadable must not stop the bot from starting."""
    path = tmp_path / "personas.json"
    path.write_text('["not", "a", "mapping"]')
    assert PersonaStore(path).get("tg:1:2") is None
