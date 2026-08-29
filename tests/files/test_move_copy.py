"""Move and copy, and the four things they refuse outright.

Delete's guards protect against losing a project. These protect against losing a
file quietly: an overwrite confirmed by a phone tap looks identical to a
successful paste, and a directory pasted into its own subtree fills the disk
before anyone notices. Neither becomes safe by being agreed to, so both are
refusals rather than confirmations — which is what these tests pin.
"""

from __future__ import annotations

from pathlib import Path

from ductor_bot.files.edits import (
    ClipboardStore,
    apply_copy,
    apply_move,
    can_move,
    can_paste,
    plan_tree,
)


def _tree(base: Path) -> tuple[Path, Path]:
    """A source directory with two files, and an empty destination."""
    src = base / "src"
    (src / "nested").mkdir(parents=True)
    (src / "a.txt").write_text("a", encoding="utf-8")
    (src / "nested" / "b.txt").write_text("bb", encoding="utf-8")
    dest = base / "dest"
    dest.mkdir()
    return src, dest


def test_copy_leaves_the_original(tmp_path: Path) -> None:
    src, dest = _tree(tmp_path)
    assert can_paste(src, dest, None, "copy") == ""

    landed = apply_copy(src, dest)

    assert landed == dest / "src"
    assert (landed / "nested" / "b.txt").read_text(encoding="utf-8") == "bb"
    assert src.exists()


def test_move_does_not(tmp_path: Path) -> None:
    src, dest = _tree(tmp_path)
    assert can_paste(src, dest, None, "move") == ""

    landed = apply_move(src, dest)

    assert (landed / "a.txt").read_text(encoding="utf-8") == "a"
    assert not src.exists()


def test_existing_name_is_refused_not_overwritten(tmp_path: Path) -> None:
    src, dest = _tree(tmp_path)
    victim = dest / "src"
    victim.mkdir()
    (victim / "precious.txt").write_text("keep me", encoding="utf-8")

    assert can_paste(src, dest, None, "copy") == "edits.paste_exists"
    assert can_paste(src, dest, None, "move") == "edits.paste_exists"
    assert (victim / "precious.txt").read_text(encoding="utf-8") == "keep me"


def test_a_folder_cannot_be_pasted_into_itself(tmp_path: Path) -> None:
    src, _ = _tree(tmp_path)

    assert can_paste(src, src, None, "copy") == "edits.paste_into_self"
    assert can_paste(src, src / "nested", None, "copy") == "edits.paste_into_self"


def test_move_into_the_same_directory_is_refused(tmp_path: Path) -> None:
    src, _ = _tree(tmp_path)

    assert can_paste(src, src.parent, None, "move") == "edits.paste_same_dir"


def test_destination_outside_the_roots_is_refused(tmp_path: Path) -> None:
    src, dest = _tree(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    roots = {"work": dest}

    assert can_paste(src, outside, roots, "copy") == "edits.paste_outside"
    assert can_paste(src, dest, roots, "copy") == ""


def test_a_configured_root_cannot_be_moved(tmp_path: Path) -> None:
    src, dest = _tree(tmp_path)
    roots = {"project": src, "work": dest}

    assert can_move(src, roots) == "edits.refuse_root"
    assert can_paste(src, dest, roots, "move") == "edits.refuse_root"
    # Copying one is only duplication, so it stays allowed.
    assert can_paste(src, dest, roots, "copy") == ""


def test_a_vanished_source_is_refused(tmp_path: Path) -> None:
    src, dest = _tree(tmp_path)
    gone = src / "not-here"

    assert can_paste(gone, dest, None, "copy") == "edits.gone"


def test_pasting_into_a_file_is_refused(tmp_path: Path) -> None:
    src, dest = _tree(tmp_path)
    a_file = dest / "file.txt"
    a_file.write_text("x", encoding="utf-8")

    assert can_paste(src, a_file, None, "copy") == "edits.paste_no_dir"


def test_symlinks_are_copied_not_followed(tmp_path: Path) -> None:
    src, dest = _tree(tmp_path)
    secret = tmp_path / "outside.txt"
    secret.write_text("not yours", encoding="utf-8")
    (src / "link").symlink_to(secret)

    landed = apply_copy(src, dest)

    assert (landed / "link").is_symlink()


def test_plan_counts_what_would_land(tmp_path: Path) -> None:
    src, _ = _tree(tmp_path)

    plan = plan_tree(src)

    assert plan.files == 2
    assert plan.bytes == 3
    assert plan.is_dir


def test_clipboard_holds_one_mark_per_conversation(tmp_path: Path) -> None:
    src, dest = _tree(tmp_path)
    store = ClipboardStore()

    store.hold("tg:1:2", "move", src)
    store.hold("tg:9:9", "copy", dest)

    assert store.get("tg:1:2").operation == "move"
    assert store.get("tg:9:9").source == dest

    store.clear("tg:1:2")
    assert store.get("tg:1:2") is None
    assert store.get("tg:9:9") is not None
