"""Where a conversation's handoff lives, and why the name is shaped that way."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ductor_bot.handoff.paths import (
    archive_dir,
    handoff_dir,
    handoff_file,
    handoff_key,
    knowledge_file,
)
from ductor_bot.session.key import SessionKey


def _paths(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(ductor_home=tmp_path / ".ductor")


def test_key_has_no_leading_minus() -> None:
    """A filename starting with '-' is read as a flag by most of coreutils."""
    key = SessionKey.telegram(chat_id=-1004326514872, topic_id=110)

    assert handoff_key(key) == "c1004326514872-t110"
    assert not handoff_key(key).startswith("-")


def test_unbound_conversation_is_general() -> None:
    key = SessionKey.telegram(chat_id=-1004326514872)

    assert handoff_key(key) == "c1004326514872-general"


def test_bound_conversation_writes_into_its_folder(tmp_path: Path) -> None:
    key = SessionKey.telegram(chat_id=-1004326514872, topic_id=110)
    folder = tmp_path / "wp-website"

    assert handoff_file(key, folder, _paths(tmp_path)) == (
        folder / "handoffs" / "c1004326514872-t110.md"
    )


def test_two_topics_sharing_a_folder_do_not_collide(tmp_path: Path) -> None:
    """Topics 97 and 110 are both bound to wp-website today."""
    folder = tmp_path / "wp-website"
    paths = _paths(tmp_path)

    a = handoff_file(SessionKey.telegram(chat_id=-100, topic_id=97), folder, paths)
    b = handoff_file(SessionKey.telegram(chat_id=-100, topic_id=110), folder, paths)

    assert a != b
    assert a.parent == b.parent


def test_unbound_falls_under_ductor_home(tmp_path: Path) -> None:
    key = SessionKey.telegram(chat_id=-1004326514872)
    paths = _paths(tmp_path)

    assert handoff_file(key, None, paths) == (
        paths.ductor_home / "handoffs" / "c1004326514872-general.md"
    )


def test_archives_live_outside_the_repo(tmp_path: Path) -> None:
    """The reliable way to stop an agent reading old state is to move it away."""
    key = SessionKey.telegram(chat_id=-100, topic_id=110)
    paths = _paths(tmp_path)
    folder = tmp_path / "proj"

    destination = archive_dir(key, paths)

    assert destination == paths.ductor_home / "handoff-archive" / "c100-t110"
    assert folder not in destination.parents


def test_project_knowledge_is_shared_by_the_folder(tmp_path: Path) -> None:
    folder = tmp_path / "wp-website"

    assert knowledge_file(folder) == folder / "handoffs" / "knowledge.md"


def test_the_directory_is_not_hidden(tmp_path: Path) -> None:
    """files/browser.py hides dotfiles, and the handoff must be readable there."""
    assert not handoff_dir(tmp_path, _paths(tmp_path)).name.startswith(".")
