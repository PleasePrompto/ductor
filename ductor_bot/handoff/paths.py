"""Where a conversation's handoff lives.

The key carries chat *and* topic because two conversations can be bound to the
same folder — topics 97 and 110 both point at the same repository today — and a
single per-repository file would have them overwriting each other's working
state silently.

The sign is stripped from the chat id. A filename beginning with ``-`` is read
as a flag by most of coreutils, so ``rm``, ``grep`` and ``cp`` all misbehave on
one, usually at the worst moment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from ductor_bot.session.key import SessionKey
    from ductor_bot.workspace.paths import DuctorPaths

#: Deliberately not a dot-directory. ``files/browser.py`` hides dotfiles, and
#: the active handoff is meant to be readable from ``/files``. Protection comes
#: from the git exclusion guard, not from being hidden.
HANDOFF_DIR_NAME = "handoffs"

#: Archives live under ductor's own home, never inside a project folder: the
#: reliable way to stop an agent reading finished working state is for it not to
#: be in the room.
ARCHIVE_DIR_NAME = "handoff-archive"

KNOWLEDGE_FILE_NAME = "knowledge.md"


def handoff_key(key: SessionKey) -> str:
    """Filename stem identifying one conversation."""
    chat = abs(key.chat_id)
    if key.topic_id is None:
        return f"c{chat}-general"
    return f"c{chat}-t{key.topic_id}"


def handoff_dir(folder: Path | None, paths: DuctorPaths) -> Path:
    """The directory holding active handoffs for a conversation.

    ``folder`` is the conversation's bound project directory, or ``None`` for
    the one unbound conversation (General), which falls under ductor's home.
    """
    base = folder if folder is not None else paths.ductor_home
    return base / HANDOFF_DIR_NAME


def handoff_file(key: SessionKey, folder: Path | None, paths: DuctorPaths) -> Path:
    """The active handoff for this conversation."""
    return handoff_dir(folder, paths) / f"{handoff_key(key)}.md"


def knowledge_file(folder: Path) -> Path:
    """Project knowledge, shared by every conversation bound to *folder*."""
    return folder / HANDOFF_DIR_NAME / KNOWLEDGE_FILE_NAME


def archive_dir(key: SessionKey, paths: DuctorPaths) -> Path:
    """Where this conversation's archived handoffs go."""
    return paths.ductor_home / ARCHIVE_DIR_NAME / handoff_key(key)
