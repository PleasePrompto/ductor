"""Binding store: consent is recorded, never inferred."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.workspace.topic_bindings import SHARED_WORKSPACE, BindingStore

KEY = "tg:-100123:7"


def test_unanswered_is_distinct_from_shared_workspace(tmp_path: Path) -> None:
    """The difference decides whether the user is asked again."""
    store = BindingStore(tmp_path / "b.json")
    assert store.has_choice(KEY) is False
    assert store.get(KEY) is None

    store.set(KEY, SHARED_WORKSPACE)
    assert store.has_choice(KEY) is True
    assert store.get(KEY) == SHARED_WORKSPACE
    assert store.resolve(KEY) is None


def test_binding_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    proj = tmp_path / "EMR"
    proj.mkdir()
    BindingStore(path).set(KEY, str(proj))

    assert BindingStore(path).resolve(KEY) == proj


def test_a_deleted_directory_stops_resolving(tmp_path: Path) -> None:
    store = BindingStore(tmp_path / "b.json")
    gone = tmp_path / "gone"
    gone.mkdir()
    store.set(KEY, str(gone))
    assert store.resolve(KEY) == gone

    gone.rmdir()
    assert store.resolve(KEY) is None
    # Still answered, so the caller can say *why* rather than asking blankly.
    assert store.has_choice(KEY) is True


def test_a_damaged_store_degrades_to_unanswered(tmp_path: Path) -> None:
    """It must never prevent startup."""
    path = tmp_path / "b.json"
    path.write_text("{not json at all")
    assert BindingStore(path).has_choice(KEY) is False


def test_held_prompt_is_returned_once(tmp_path: Path) -> None:
    store = BindingStore(tmp_path / "b.json")
    store.hold(KEY, "do the thing")
    assert store.take(KEY) == "do the thing"
    assert store.take(KEY) is None


def test_held_prompts_do_not_persist(tmp_path: Path) -> None:
    """Replaying an instruction after a restart, unwatched, is worse than re-asking."""
    path = tmp_path / "b.json"
    store = BindingStore(path)
    store.set(KEY, SHARED_WORKSPACE)
    store.hold(KEY, "delete everything")

    assert BindingStore(path).take(KEY) is None


def test_bindings_are_per_key(tmp_path: Path) -> None:
    store = BindingStore(tmp_path / "b.json")
    store.set("tg:-100:1", "/a")
    store.set("tg:-100:2", "/b")
    assert store.get("tg:-100:1") == "/a"
    assert store.has_choice("tg:-100:3") is False


def test_clear_forgets_the_binding_and_the_hold(tmp_path: Path) -> None:
    store = BindingStore(tmp_path / "b.json")
    store.set(KEY, "/a")
    store.hold(KEY, "x")
    store.clear(KEY)
    assert store.has_choice(KEY) is False
    assert store.take(KEY) is None
