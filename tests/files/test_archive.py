"""Safety checks for archives arriving from chat.

A zip sent to the bot is untrusted input that gets written to a directory the
user picked, so every check here is about what happens *before* anything is
written to that directory.
"""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from ductor_bot.files.archive import (
    MAX_ENTRIES,
    MAX_TOTAL_BYTES,
    extract_archive,
    inspect_archive,
)


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_lists_entries(tmp_path: Path) -> None:
    src = _zip(tmp_path / "a.zip", {"notes.md": b"hi", "sub/deep.txt": b"x" * 10})
    entries, err = inspect_archive(src)
    assert err == ""
    assert entries is not None
    assert sorted(e.name for e in entries) == ["notes.md", "sub/deep.txt"]
    assert {e.size for e in entries} == {2, 10}


def test_directory_entries_are_not_listed(tmp_path: Path) -> None:
    src = tmp_path / "d.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("folder/", b"")
        zf.writestr("folder/f.txt", b"ok")
    entries, err = inspect_archive(src)
    assert err == ""
    assert entries is not None
    assert [e.name for e in entries] == ["folder/f.txt"]


def test_rejects_non_zip(tmp_path: Path) -> None:
    bogus = tmp_path / "not.zip"
    bogus.write_bytes(b"definitely not a zip")
    entries, err = inspect_archive(bogus)
    assert entries is None
    assert err == "upload.not_a_zip"


@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "sub/../../escape.txt",
        "/etc/passwd",
        "/root/.ssh/authorized_keys",
    ],
)
def test_rejects_path_traversal(tmp_path: Path, name: str) -> None:
    """The whole archive fails: a partial extraction of a hostile zip is worse."""
    src = _zip(tmp_path / "evil.zip", {name: b"x", "innocent.txt": b"y"})
    entries, err = inspect_archive(src)
    assert entries is None
    assert err == "upload.zip_unsafe"


def test_rejects_symlink_entries(tmp_path: Path) -> None:
    src = tmp_path / "link.zip"
    with zipfile.ZipFile(src, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    entries, err = inspect_archive(src)
    assert entries is None
    assert err == "upload.zip_unsafe"


def test_rejects_too_many_entries(tmp_path: Path) -> None:
    src = _zip(tmp_path / "many.zip", {f"f{i}.txt": b"." for i in range(MAX_ENTRIES + 1)})
    entries, err = inspect_archive(src)
    assert entries is None
    assert err == "upload.zip_too_many"


def test_rejects_decompression_bomb(tmp_path: Path) -> None:
    """Checked from the header, so the bytes are never written to disk."""
    src = tmp_path / "bomb.zip"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb", b"\0" * (MAX_TOTAL_BYTES + 1))
    entries, err = inspect_archive(src)
    assert entries is None
    assert err == "upload.zip_too_big"
    assert src.stat().st_size < 1024 * 1024, "the test archive should be tiny"


def test_extract_writes_the_tree(tmp_path: Path) -> None:
    src = _zip(tmp_path / "ok.zip", {"a.txt": b"one", "sub/b.txt": b"two"})
    dest = tmp_path / "out"
    count = extract_archive(src, dest)
    assert count == 2
    assert (dest / "a.txt").read_bytes() == b"one"
    assert (dest / "sub" / "b.txt").read_bytes() == b"two"


def test_extract_refuses_an_archive_that_failed_inspection(tmp_path: Path) -> None:
    src = _zip(tmp_path / "evil.zip", {"../escape.txt": b"x"})
    dest = tmp_path / "out"
    with pytest.raises(ValueError, match="unsafe"):
        extract_archive(src, dest)
    assert not (tmp_path / "escape.txt").exists()
