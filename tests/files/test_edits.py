"""Guards on the destructive browser operations.

Most of these assert a refusal. A confirmation dialog is not protection when
the cost of being wrong is an entire project, so the two worst outcomes —
deleting a configured root, deleting a git repository — are refused outright
rather than confirmed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ductor_bot.files.edits import (
    apply_delete,
    apply_newdir,
    apply_rename,
    can_delete,
    can_rename,
    is_repository,
    is_root,
    plan_delete,
    sample,
    validate_name,
)


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    root = tmp_path / "EMR"
    (root / "apps" / "web").mkdir(parents=True)
    (root / "apps" / "web" / "index.html").write_text("<html>")
    (root / "README.md").write_text("readme")
    return root, {"EMR": root}


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("", "edits.name_empty"),
        ("   ", "edits.name_empty"),
        ("a/b", "edits.name_illegal"),
        ("a\\b", "edits.name_illegal"),
        ("..", "edits.name_traversal"),
        ("../escape", "edits.name_illegal"),
        (".", "edits.name_traversal"),
        ("x" * 101, "edits.name_too_long"),
    ],
)
def test_dangerous_names_are_refused(name: str, key: str) -> None:
    assert validate_name(name) == key


@pytest.mark.parametrize("name", ["notes", "My Folder", "v2.1", ".hidden", "a-b_c"])
def test_ordinary_names_are_accepted(name: str) -> None:
    assert validate_name(name) == ""


def test_a_name_cannot_escape_its_directory() -> None:
    """The whole attack surface of a text field that names a path."""
    assert validate_name("../../etc/passwd") != ""
    assert validate_name("/etc/passwd") != ""


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_root_cannot_be_deleted(tree) -> None:
    """Deleting EMR/ removes an entire project; no dialog makes that safe."""
    root, roots = tree
    assert can_delete(root, roots) == "edits.refuse_root"


def test_a_root_cannot_be_renamed(tree) -> None:
    root, roots = tree
    assert can_rename(root, roots) == "edits.refuse_root"


def test_a_repository_cannot_be_deleted(tree) -> None:
    """Untracked files in a repo exist nowhere else."""
    root, roots = tree
    repo = root / "apps" / "web"
    (repo / ".git").mkdir()
    assert is_repository(repo) is True
    assert can_delete(repo, roots) == "edits.refuse_repo"


def test_an_ordinary_folder_can_be_deleted(tree) -> None:
    root, roots = tree
    assert can_delete(root / "apps" / "web", roots) == ""


def test_a_missing_target_is_reported(tree) -> None:
    root, roots = tree
    assert can_delete(root / "nope", roots) == "edits.gone"


def test_a_repository_can_still_be_renamed(tree) -> None:
    """Renaming is reversible; deleting is not."""
    root, roots = tree
    repo = root / "apps" / "web"
    (repo / ".git").mkdir()
    assert can_rename(repo, roots) == ""


def test_root_detection_survives_a_trailing_slash(tree) -> None:
    root, roots = tree
    assert is_root(Path(str(root) + "/"), roots) is True


# ---------------------------------------------------------------------------
# What the user is told before confirming
# ---------------------------------------------------------------------------


def test_plan_counts_the_whole_tree(tree) -> None:
    root, _roots = tree
    plan = plan_delete(root / "apps")
    assert plan.is_dir is True
    assert plan.files == 1
    assert plan.bytes == len("<html>")


def test_plan_for_a_single_file(tree) -> None:
    root, _roots = tree
    plan = plan_delete(root / "README.md")
    assert (plan.is_dir, plan.files) == (False, 1)


def test_plan_does_not_follow_symlinks(tmp_path: Path) -> None:
    """A link into a project must not make its contents look deletable."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x" * 500)
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "link").symlink_to(outside)

    plan = plan_delete(folder)
    assert plan.files == 0
    assert plan.bytes == 0


def test_sample_lists_children(tree) -> None:
    root, _roots = tree
    assert sample(root) == ["README.md", "apps/"]


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def test_rename_moves_in_place(tree) -> None:
    root, _roots = tree
    out = apply_rename(root / "README.md", "READ.md")
    assert out == root / "READ.md"
    assert not (root / "README.md").exists()


def test_rename_refuses_to_clobber(tree) -> None:
    root, _roots = tree
    (root / "taken.md").write_text("mine")
    with pytest.raises(FileExistsError):
        apply_rename(root / "README.md", "taken.md")
    assert (root / "taken.md").read_text() == "mine"


def test_newdir_creates_one_level(tree) -> None:
    root, _roots = tree
    out = apply_newdir(root, "docs")
    assert out.is_dir()


def test_newdir_refuses_to_clobber(tree) -> None:
    root, _roots = tree
    with pytest.raises(FileExistsError):
        apply_newdir(root, "apps")


def test_delete_removes_a_tree(tree) -> None:
    root, _roots = tree
    apply_delete(root / "apps")
    assert not (root / "apps").exists()
    assert (root / "README.md").exists(), "siblings untouched"


def test_delete_of_a_symlink_does_not_touch_the_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("important")
    link = tmp_path / "link"
    link.symlink_to(outside)

    apply_delete(link)
    assert not link.exists()
    assert (outside / "keep.txt").read_text() == "important"


def test_a_configured_root_nested_inside_another_is_still_protected(tmp_path: Path) -> None:
    """browsable_roots collapses nested entries; the guard must not.

    With `IT` configured, every project under it disappears from the browsable
    set — so checking only that set would leave each project deletable.
    """
    umbrella = tmp_path / "IT"
    project = umbrella / "EMR"
    project.mkdir(parents=True)

    collapsed_only = {"IT": umbrella}
    assert can_delete(project, collapsed_only) == "", "collapsed set does not protect it"

    all_configured = {"IT": umbrella, "cfg:EMR": project}
    assert can_delete(project, all_configured) == "edits.refuse_root"
