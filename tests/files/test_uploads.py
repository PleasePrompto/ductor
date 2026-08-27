"""Staging behaviour: nothing reaches the destination without a commit."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.files.uploads import UploadStore, plan

KEY = "tg:-100123:7"


def _store(tmp_path: Path) -> tuple[UploadStore, Path]:
    dest = tmp_path / "project"
    dest.mkdir()
    return UploadStore(tmp_path / "staging"), dest


def test_commit_moves_files_and_closes_the_session(tmp_path: Path) -> None:
    store, dest = _store(tmp_path)
    session = store.begin(KEY, dest, "files")
    (session.staging / "a.txt").write_text("one")
    (session.staging / "b.txt").write_text("two")

    assert store.commit(KEY) == 2
    assert (dest / "a.txt").read_text() == "one"
    assert store.get(KEY) is None
    assert not session.staging.exists()


def test_commit_preserves_nested_structure(tmp_path: Path) -> None:
    """Folder mode stages an extracted tree; the tree has to survive the move."""
    store, dest = _store(tmp_path)
    session = store.begin(KEY, dest, "folder")
    (session.staging / "sub").mkdir()
    (session.staging / "sub" / "deep.txt").write_text("x")

    assert store.commit(KEY) == 1
    assert (dest / "sub" / "deep.txt").read_text() == "x"


def test_cancel_leaves_the_destination_untouched(tmp_path: Path) -> None:
    store, dest = _store(tmp_path)
    session = store.begin(KEY, dest, "files")
    (session.staging / "unwanted.txt").write_text("nope")

    store.end(KEY)
    assert list(dest.iterdir()) == []
    assert not session.staging.exists()


def test_plan_flags_overwrites(tmp_path: Path) -> None:
    store, dest = _store(tmp_path)
    (dest / "notes.md").write_text("original")
    session = store.begin(KEY, dest, "files")
    (session.staging / "notes.md").write_text("replacement")
    (session.staging / "fresh.txt").write_text("new")

    items = {i.name: i for i in plan(session)}
    assert items["notes.md"].overwrites is True
    assert items["fresh.txt"].overwrites is False
    assert items["fresh.txt"].size == 3
    # Still nothing written until commit.
    assert (dest / "notes.md").read_text() == "original"


def test_begin_replaces_an_open_session(tmp_path: Path) -> None:
    store, dest = _store(tmp_path)
    first = store.begin(KEY, dest, "files")
    (first.staging / "stale.txt").write_text("old")

    second = store.begin(KEY, dest / "elsewhere", "folder")
    assert plan(second) == []
    assert store.commit(KEY) == 0


def test_sessions_are_isolated_per_key(tmp_path: Path) -> None:
    store, dest = _store(tmp_path)
    a = store.begin("tg:1", dest, "files")
    b = store.begin("tg:2", dest, "files")
    assert a.staging != b.staging

    (a.staging / "a.txt").write_text("a")
    store.end("tg:1")
    assert store.get("tg:2") is not None


def test_commit_on_a_closed_session_is_a_no_op(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    assert store.commit("never-opened") == 0
