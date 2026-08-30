"""Reading, writing and archiving handoffs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from ductor_bot.handoff.store import HandoffStore
from ductor_bot.session.key import SessionKey

KEY = SessionKey.telegram(chat_id=-100, topic_id=110)
OTHER = SessionKey.telegram(chat_id=-100, topic_id=97)


def _store(tmp_path: Path) -> HandoffStore:
    return HandoffStore(SimpleNamespace(ductor_home=tmp_path / ".ductor"))


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    folder.mkdir(exist_ok=True)
    return folder


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    folder = _folder(tmp_path)

    assert store.write(KEY, folder, "# Handoff\n\n## Objective\nship it\n")

    assert "ship it" in store.read(KEY, folder)


def test_reading_a_missing_handoff_is_empty(tmp_path: Path) -> None:
    assert _store(tmp_path).read(KEY, _folder(tmp_path)) == ""


def test_archive_moves_the_file_out_of_the_folder(tmp_path: Path) -> None:
    store = _store(tmp_path)
    folder = _folder(tmp_path)
    store.write(KEY, folder, "## Objective\nwebsite redesign\n")

    archived = store.archive(KEY, folder)

    assert archived is not None
    assert archived.exists()
    assert not (folder / "handoffs" / "c100-t110.md").exists()
    assert "website redesign" in archived.read_text(encoding="utf-8")
    assert folder not in archived.parents


def test_archiving_nothing_is_not_an_error(tmp_path: Path) -> None:
    assert _store(tmp_path).archive(KEY, _folder(tmp_path)) is None


def test_archives_are_listed_newest_first_and_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    folder = _folder(tmp_path)
    store.write(KEY, folder, "## Objective\nfirst task\n")
    store.archive(KEY, folder)
    store.write(KEY, folder, "## Objective\nsecond task\n")
    store.archive(KEY, folder)
    store.write(OTHER, folder, "## Objective\nsomeone elses topic\n")
    store.archive(OTHER, folder)

    found = store.list_archives(KEY)

    assert len(found) == 2
    bodies = [p.read_text(encoding="utf-8") for p in found]
    assert all("someone elses topic" not in b for b in bodies)


def test_an_empty_consolidation_never_replaces_a_good_handoff(tmp_path: Path) -> None:
    """A model returning nothing is a failed turn, not an order to forget."""
    store = _store(tmp_path)
    folder = _folder(tmp_path)
    store.write(KEY, folder, "## Objective\nreal work\n")

    assert not store.write(KEY, folder, "   \n")

    assert "real work" in store.read(KEY, folder)


def test_a_write_into_a_repo_is_protected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    folder = _folder(tmp_path)
    subprocess.run(["git", "init", "-q", str(folder)], check=True)

    assert store.write(KEY, folder, "## Objective\nship it\n")

    exclude = (folder / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "handoffs/" in exclude


def test_an_unbound_conversation_writes_under_ductor_home(tmp_path: Path) -> None:
    store = _store(tmp_path)
    general = SessionKey.telegram(chat_id=-100)

    assert store.write(general, None, "## Objective\ngeneral chatter\n")

    assert "general chatter" in store.read(general, None)
