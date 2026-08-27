"""Staging for files uploaded through the file browser.

Uploads land in a staging directory first and are only moved into the target
folder once the user confirms. Two reasons that ordering matters: a file sent
by mistake never touches the working directory, and the confirmation is the
one moment where an overwrite can be reported before it destroys anything.

Sessions live in memory. A restart drops them, which downgrades the next
upload to ordinary media handling — visible to the user and harmless, unlike
persisting a half-finished upload across a version change.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Mode = Literal["files", "folder"]


@dataclass(frozen=True, slots=True)
class StagedItem:
    """One file waiting to be moved, and whether it would replace something."""

    name: str
    size: int
    overwrites: bool


@dataclass(slots=True)
class UploadSession:
    """An open upload, addressed by session key."""

    dest: Path
    mode: Mode
    staging: Path
    #: The message being edited as files arrive, so the list stays in one place
    #: instead of pushing a new message per file.
    message_id: int | None = None
    #: Name of the archive being reviewed, in folder mode. A second archive is
    #: refused while this is set rather than interleaving two extractions.
    archive: str | None = None
    errors: list[str] = field(default_factory=list)


class UploadStore:
    """In-memory registry of open uploads, one per session key."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._sessions: dict[str, UploadSession] = {}

    def begin(self, key: str, dest: Path, mode: Mode) -> UploadSession:
        """Open an upload for *key*, replacing any session already open."""
        self.end(key)
        staging = self._root / _slug(key)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        session = UploadSession(dest=dest, mode=mode, staging=staging)
        self._sessions[key] = session
        return session

    def get(self, key: str) -> UploadSession | None:
        return self._sessions.get(key)

    def end(self, key: str) -> None:
        """Close the upload for *key* and discard anything staged."""
        session = self._sessions.pop(key, None)
        if session is not None:
            shutil.rmtree(session.staging, ignore_errors=True)

    def commit(self, key: str) -> int:
        """Move everything staged for *key* into its destination.

        Returns the number of files moved. The session is closed either way,
        so a confirm always ends the upload.
        """
        session = self._sessions.get(key)
        if session is None:
            return 0
        moved = 0
        try:
            for src in sorted(_staged_files(session.staging)):
                out = session.dest / src.relative_to(session.staging)
                out.parent.mkdir(parents=True, exist_ok=True)
                # Path.replace would fail across filesystems; staging shares a
                # volume with the target today, but shutil.move is correct
                # either way and costs nothing when it is a rename.
                shutil.move(str(src), str(out))
                moved += 1
        finally:
            self.end(key)
        return moved


def plan(session: UploadSession) -> list[StagedItem]:
    """What a confirmation would move, and what it would overwrite."""
    items: list[StagedItem] = []
    for path in sorted(_staged_files(session.staging)):
        rel = path.relative_to(session.staging)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        items.append(
            StagedItem(name=str(rel), size=size, overwrites=(session.dest / rel).exists())
        )
    return items


def _staged_files(staging: Path) -> list[Path]:
    return [p for p in staging.rglob("*") if p.is_file()]


def _slug(key: str) -> str:
    """Filesystem-safe directory name for a session key.

    Session keys contain colons, which are legal on Linux and not elsewhere;
    hashing sidesteps that and keeps chat identifiers out of directory names.
    """
    return hashlib.sha256(key.encode()).hexdigest()[:16]
