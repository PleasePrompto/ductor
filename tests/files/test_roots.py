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


def test_home_and_projects_are_browsable(tmp_path: Path) -> None:
    home, roots = _tree(tmp_path)
    result = browsable_roots(home, roots)
    assert "~/.ductor" in result
    assert set(result) >= {"IT", "EMR", "Phoenix-MT5"}


def test_missing_directories_are_dropped(tmp_path: Path) -> None:
    """A root that cannot be opened reads as a broken browser."""
    home, roots = _tree(tmp_path)
    assert "gone" not in browsable_roots(home, roots)


def test_duplicate_directories_collapse(tmp_path: Path) -> None:
    """Two topic names mapped at one folder should not list it twice."""
    home, roots = _tree(tmp_path)
    roots["EMR-alias"] = roots["EMR"]
    result = browsable_roots(home, roots)
    assert sum(1 for p in result.values() if p == Path(roots["EMR"]).resolve()) == 1


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


def test_label_prefers_the_deepest_matching_root(tmp_path: Path) -> None:
    """EMR sits inside IT; the more specific name is the useful one."""
    home, roots = _tree(tmp_path)
    result = browsable_roots(home, roots)
    label, _ = label_for(result, tmp_path / "IT" / "EMR")
    assert label == "EMR"
