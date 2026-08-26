"""Tests for the browsable-root allowlist."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.files.roots import browsable_roots, contains, label_for


def _tree(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = tmp_path / ".ductor"
    home.mkdir()
    (tmp_path / "IT" / "EMR").mkdir(parents=True)
    (tmp_path / "IT" / "Phoenix" / "Phoenix-MT5").mkdir(parents=True)
    return home, {
        "IT": str(tmp_path / "IT"),
        "EMR": str(tmp_path / "IT" / "EMR"),
        "Phoenix-MT5": str(tmp_path / "IT" / "Phoenix" / "Phoenix-MT5"),
        "gone": str(tmp_path / "does-not-exist"),
    }


def test_home_and_top_level_projects_are_browsable(tmp_path: Path) -> None:
    home, roots = _tree(tmp_path)
    result = browsable_roots(home, roots)
    assert "~/.ductor" in result
    assert "IT" in result


def test_nested_roots_collapse_into_their_ancestor(tmp_path: Path) -> None:
    """Mapping IT, IT/EMR and IT/Phoenix/Phoenix-MT5 should offer one entry.

    Listing all three at the top level flattens the tree into the root view and
    makes navigation pointless — the nested ones are reachable by opening IT.
    """
    home, roots = _tree(tmp_path)
    result = browsable_roots(home, roots)
    assert "EMR" not in result
    assert "Phoenix-MT5" not in result
    assert set(result) == {"~/.ductor", "IT"}


def test_unnested_projects_all_appear(tmp_path: Path) -> None:
    """Collapsing must not hide roots that are genuinely separate."""
    home = tmp_path / ".ductor"
    home.mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    result = browsable_roots(
        home, {"alpha": str(tmp_path / "alpha"), "beta": str(tmp_path / "beta")}
    )
    assert set(result) == {"~/.ductor", "alpha", "beta"}


def test_missing_directories_are_dropped(tmp_path: Path) -> None:
    """A root that cannot be opened reads as a broken browser."""
    home, roots = _tree(tmp_path)
    assert "gone" not in browsable_roots(home, roots)


def test_duplicate_directories_collapse(tmp_path: Path) -> None:
    """Two topic names mapped at one folder should not list it twice."""
    home = tmp_path / ".ductor"
    home.mkdir()
    (tmp_path / "solo").mkdir()
    result = browsable_roots(
        home, {"solo": str(tmp_path / "solo"), "solo-alias": str(tmp_path / "solo")}
    )
    assert sum(1 for p in result.values() if p == (tmp_path / "solo").resolve()) == 1


def test_contains_rejects_paths_outside_every_root(tmp_path: Path) -> None:
    home, roots = _tree(tmp_path)
    result = browsable_roots(home, roots)
    assert contains(result, tmp_path / "IT" / "EMR")
    assert not contains(result, tmp_path / "elsewhere")
    assert not contains(result, Path("/etc"))


def test_traversal_outside_a_root_is_refused(tmp_path: Path) -> None:
    home, roots = _tree(tmp_path)
    result = browsable_roots(home, roots)
    assert not contains(result, tmp_path / "IT" / "EMR" / ".." / ".." / ".." / "etc")


def test_label_names_the_owning_root(tmp_path: Path) -> None:
    """A path inside IT is labelled with IT, so breadcrumbs read IT/EMR/src."""
    home, roots = _tree(tmp_path)
    result = browsable_roots(home, roots)
    label, _ = label_for(result, tmp_path / "IT" / "EMR" / "src")
    assert label == "IT"
