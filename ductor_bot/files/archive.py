"""Inspection and extraction of archives received from chat.

A zip arriving in a message is untrusted input that the user then asks to be
written into a working directory, so it is validated in full before a single
byte is written. Anything suspect fails the whole archive: extracting the
harmless half of a hostile zip leaves the user with a mess and no clear signal
that something was wrong.

``zipfile`` sanitises ``..`` and absolute paths on its own, but it reports
nothing about why, restores symlink entries as ordinary files, and has no
notion of a decompression bomb. The checks here are explicit so the reason for
a rejection can be shown to the user and so none of that behaviour is taken on
trust.
"""

from __future__ import annotations

import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: Bots can only download files up to 20 MB, so a legitimate archive is small.
#: The ceiling exists to stop a bomb filling the volume the bot shares with
#: everything else on the host.
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_ENTRIES = 2000


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """One regular file inside an archive."""

    name: str
    size: int


def inspect_archive(path: Path) -> tuple[list[ArchiveEntry] | None, str]:
    """Validate *path* and list its files.

    Returns ``(entries, "")`` when the archive is safe to extract, or
    ``(None, error_key)`` naming the translation for the refusal.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError):
        return None, "upload.not_a_zip"

    entries: list[ArchiveEntry] = []
    total = 0
    for info in infos:
        if info.is_dir():
            continue
        name = _safe_name(info.filename) if _is_regular_file(info) else None
        if name is None:
            return None, "upload.zip_unsafe"
        total += info.file_size
        entries.append(ArchiveEntry(name=name, size=info.file_size))

    if total > MAX_TOTAL_BYTES:
        return None, "upload.zip_too_big"
    if len(entries) > MAX_ENTRIES:
        return None, "upload.zip_too_many"
    if not entries:
        return None, "upload.zip_empty"
    return entries, ""


def extract_archive(path: Path, dest: Path) -> int:
    """Extract the validated contents of *path* into *dest*. Returns the count.

    Re-runs the inspection rather than trusting the caller to have done it:
    this is the function that writes to disk, so it is the one that has to be
    certain.
    """
    entries, error_key = inspect_archive(path)
    if entries is None:
        msg = f"refusing to extract unsafe archive: {error_key}"
        raise ValueError(msg)

    dest.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest.resolve()
    with zipfile.ZipFile(path) as zf:
        for entry in entries:
            out = (dest / entry.name).resolve()
            # The name was checked, but the join is what actually decides where
            # the bytes go — a symlinked parent could still redirect it.
            if not out.is_relative_to(resolved_dest):
                msg = f"refusing to extract unsafe archive: {entry.name}"
                raise ValueError(msg)
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(entry.name) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    return len(entries)


def _is_regular_file(info: zipfile.ZipInfo) -> bool:
    """False for symlinks, devices and anything else that is not a plain file.

    Only the file-type field is consulted. Plenty of legitimate writers store
    permission bits alone (``0o600``) or nothing at all — neither names a type,
    and both mean "ordinary file". A populated type field that is not
    ``S_IFREG`` is the case worth refusing.
    """
    file_type = stat.S_IFMT(info.external_attr >> 16)
    return file_type in (0, stat.S_IFREG)


def _safe_name(name: str) -> str | None:
    """Return *name* as a safe relative path, or None if it escapes."""
    if not name or name.startswith("/") or "\\" in name:
        return None
    # Windows drive letters ("C:/x") are absolute despite not starting with "/".
    if len(name) > 1 and name[1] == ":":
        return None
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        return None
    return str(pure)
