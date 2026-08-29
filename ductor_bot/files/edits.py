"""Rename, create, delete, move and copy from the file browser.

Every operation here changes the user's files, and some cannot be undone. The
guards are the point of the module; the operations themselves are three lines
each.

These refusals are absolute rather than confirmable:

* a configured root cannot be deleted, renamed or moved — it is an entire
  project, and the browser offers no way to put it back;
* a directory containing ``.git`` cannot be deleted — it is a repository, and
  its untracked files exist nowhere else;
* a paste never overwrites — a dialog offering to replace is how a phone tap
  destroys a file that took a day to produce;
* a directory cannot be pasted inside its own subtree, which would recurse
  until the disk filled.

A confirmation dialog is not protection against any of these. Someone tapping
twice on a phone has not reviewed a thousand files, and the cost of being wrong
is a project rather than a folder.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Kind = Literal["rename", "newdir", "delete"]

#: Move and copy are one operation with two endings, so they share a clipboard
#: and every guard below.
Operation = Literal["move", "copy"]

#: Rejected outright: separators and traversal would let a typed name escape
#: the directory being edited, which is the whole attack surface of a text
#: field that names a path.
_ILLEGAL = re.compile(r"[/\\\x00]")
_MAX_NAME = 100

#: Deleting more than this without reading the list is not a decision. The
#: browser shows a sample; past this it also refuses to proceed silently.
LARGE_DELETE = 50


@dataclass(frozen=True, slots=True)
class DeletePlan:
    """What a delete would remove, so the user can be told before confirming."""

    files: int
    bytes: int
    is_dir: bool


def validate_name(name: str) -> str:
    """Return "" if *name* is usable, else a translation key explaining why."""
    name = name.strip()
    if not name:
        return "edits.name_empty"
    if len(name) > _MAX_NAME:
        return "edits.name_too_long"
    if _ILLEGAL.search(name):
        return "edits.name_illegal"
    if name in {".", ".."} or name.startswith(".."):
        return "edits.name_traversal"
    return ""


def is_root(target: Path, roots: dict[str, Path] | None) -> bool:
    """True when *target* is one of the browsable roots themselves."""
    if not roots:
        return False
    resolved = target.resolve()
    return any(resolved == root.resolve() for root in roots.values())


def is_repository(target: Path) -> bool:
    """True when *target* is the top of a git repository."""
    return (target / ".git").exists()


def can_delete(target: Path, roots: dict[str, Path] | None) -> str:
    """"" if *target* may be deleted, else a translation key for the refusal."""
    if not target.exists():
        return "edits.gone"
    if is_root(target, roots):
        return "edits.refuse_root"
    if target.is_dir() and is_repository(target):
        return "edits.refuse_repo"
    return ""


def can_rename(target: Path, roots: dict[str, Path] | None) -> str:
    """"" if *target* may be renamed, else a translation key for the refusal."""
    if not target.exists():
        return "edits.gone"
    if is_root(target, roots):
        return "edits.refuse_root"
    return ""


def plan_delete(target: Path) -> DeletePlan:
    """Count what a delete would remove. Never follows symlinks."""
    if target.is_file() or target.is_symlink():
        size = target.stat().st_size if target.is_file() else 0
        return DeletePlan(files=1, bytes=size, is_dir=False)

    files = 0
    total = 0
    for item in target.rglob("*"):
        if item.is_symlink() or not item.is_file():
            continue
        files += 1
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return DeletePlan(files=files, bytes=total, is_dir=True)


def sample(target: Path, limit: int = 12) -> list[str]:
    """A readable sample of what is inside, for the confirmation screen."""
    if not target.is_dir():
        return [target.name]
    names = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    return names[:limit]


#: Copy and delete both answer "how many files, how many bytes", and the answer
#: is computed identically. ``plan_delete`` is the older name; this alias exists
#: so a paste screen does not read as though it were about to delete something.
plan_tree = plan_delete


def can_move(target: Path, roots: dict[str, Path] | None) -> str:
    """"" if *target* may be moved, else a translation key for the refusal.

    A configured root is refused for the same reason rename refuses it: moving
    one relocates an entire project out from under every binding that names it.
    """
    if not target.exists():
        return "edits.gone"
    if is_root(target, roots):
        return "edits.refuse_root"
    return ""


def _is_within(child: Path, parent: Path) -> bool:
    """True when *child* is *parent* or sits underneath it, without touching disk."""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def can_paste(  # noqa: PLR0911
    source: Path,
    destination_dir: Path,
    roots: dict[str, Path] | None,
    operation: Operation,
) -> str:
    """"" if *source* may be pasted into *destination_dir*, else a refusal key.

    These are refusals rather than confirmations. A dialog offering to overwrite
    is how a phone tap silently destroys a file that took a day to produce, and
    a directory pasted into its own subtree recurses until the disk is full —
    neither becomes safe by being agreed to.
    """
    if not source.exists():
        return "edits.gone"
    if not destination_dir.is_dir():
        return "edits.paste_no_dir"

    src = source.resolve()
    dest = destination_dir.resolve()

    if roots and not any(_is_within(dest, root.resolve()) for root in roots.values()):
        return "edits.paste_outside"
    # Order matters: a move into the source's own directory would also trip the
    # name check below, and "it is already there" explains it while "something
    # with that name exists" sends someone hunting for a conflict that is the
    # file itself.
    if operation == "move":
        if dest == src.parent:
            return "edits.paste_same_dir"
        if is_root(source, roots):
            return "edits.refuse_root"
    if src.is_dir() and _is_within(dest, src):
        return "edits.paste_into_self"
    if (dest / source.name).exists():
        return "edits.paste_exists"
    return ""


def apply_move(source: Path, destination_dir: Path) -> Path:
    """Move *source* into *destination_dir*. Callers check ``can_paste`` first."""
    destination = destination_dir / source.name
    if destination.exists():
        raise FileExistsError(destination)
    shutil.move(str(source), str(destination))
    return destination


def apply_copy(source: Path, destination_dir: Path) -> Path:
    """Copy *source* into *destination_dir*, never following symlinks.

    A symlink is copied as a symlink. Following one would silently duplicate
    whatever it points at, which may be outside the browsable roots entirely.
    """
    destination = destination_dir / source.name
    if destination.exists():
        raise FileExistsError(destination)
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)
    return destination


def apply_rename(target: Path, new_name: str) -> Path:
    """Rename *target* in place. Raises FileExistsError if taken."""
    destination = target.parent / new_name
    if destination.exists():
        raise FileExistsError(destination)
    target.rename(destination)
    return destination


def apply_newdir(parent: Path, name: str) -> Path:
    """Create a subdirectory. Raises FileExistsError if taken."""
    destination = parent / name
    destination.mkdir(parents=False, exist_ok=False)
    return destination


def apply_delete(target: Path) -> None:
    """Remove *target*. Callers must have checked ``can_delete`` first."""
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()


@dataclass(slots=True)
class PendingEdit:
    """A rename or folder creation waiting for its name, then for confirmation.

    Two stages on purpose. The name arrives as an ordinary chat message, which
    is the only way Telegram can collect text, so the confirmation screen is
    what proves the bot understood it before anything is written.
    """

    kind: Kind
    target: Path
    name: str = ""
    message_id: int | None = None


@dataclass(frozen=True, slots=True)
class Clipped:
    """What was marked for a move or a copy, and which of the two it is."""

    operation: Operation
    source: Path


class ClipboardStore:
    """One pending move or copy per conversation. Memory only.

    Not persisted, for the same reason ``EditStore`` is not: a move marked
    before a restart, pasted after one, would act on a directory the user has
    long since navigated away from and no longer has on screen.
    """

    def __init__(self) -> None:
        self._clipped: dict[str, Clipped] = {}

    def hold(self, key: str, operation: Operation, source: Path) -> Clipped:
        clipped = Clipped(operation=operation, source=source)
        self._clipped[key] = clipped
        return clipped

    def get(self, key: str) -> Clipped | None:
        return self._clipped.get(key)

    def clear(self, key: str) -> None:
        self._clipped.pop(key, None)


class EditStore:
    """Edits in progress, one per conversation. Memory only.

    Not persisted: a rename half-answered before a restart should be forgotten,
    not resumed later against a directory the user has stopped looking at.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingEdit] = {}

    def begin(self, key: str, kind: Kind, target: Path) -> PendingEdit:
        edit = PendingEdit(kind=kind, target=target)
        self._pending[key] = edit
        return edit

    def get(self, key: str) -> PendingEdit | None:
        return self._pending.get(key)

    def end(self, key: str) -> None:
        self._pending.pop(key, None)
