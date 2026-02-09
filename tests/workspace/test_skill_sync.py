"""Tests for cross-platform skill directory sync."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from ductor_bot.workspace.paths import DuctorPaths
from ductor_bot.workspace.skill_sync import (
    _clean_broken_links,
    _discover_skills,
    _ensure_link,
    _resolve_canonical,
    sync_skills,
    watch_skill_sync,
)


def _make_paths(tmp_path: Path) -> DuctorPaths:
    return DuctorPaths(
        ductor_home=tmp_path / "ductor_home",
        home_defaults=tmp_path / "fw" / "workspace",
        framework_root=tmp_path / "fw",
    )


def _make_skill(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}")
    return d


# ---------------------------------------------------------------------------
# Group 1: _discover_skills
# ---------------------------------------------------------------------------


def test_discover_empty_dir(tmp_path: Path) -> None:
    d = tmp_path / "skills"
    d.mkdir()
    assert _discover_skills(d) == {}


def test_discover_real_dirs(tmp_path: Path) -> None:
    base = tmp_path / "skills"
    _make_skill(base, "my-skill")
    _make_skill(base, "other-skill")
    result = _discover_skills(base)
    assert set(result.keys()) == {"my-skill", "other-skill"}
    assert all(not v.is_symlink() for v in result.values())


def test_discover_skips_internal_dirs(tmp_path: Path) -> None:
    base = tmp_path / "skills"
    for name in (".system", ".claude", ".git", ".venv", "__pycache__", "node_modules"):
        (base / name).mkdir(parents=True)
    _make_skill(base, "real-skill")
    result = _discover_skills(base)
    assert list(result.keys()) == ["real-skill"]


def test_discover_includes_valid_symlinks(tmp_path: Path) -> None:
    base = tmp_path / "skills"
    base.mkdir()
    real = _make_skill(tmp_path / "external", "shared")
    (base / "shared").symlink_to(real)
    result = _discover_skills(base)
    assert "shared" in result
    assert result["shared"].is_symlink()


def test_discover_excludes_broken_symlinks(tmp_path: Path) -> None:
    base = tmp_path / "skills"
    base.mkdir()
    (base / "broken").symlink_to(tmp_path / "nonexistent")
    result = _discover_skills(base)
    assert result == {}


def test_discover_nonexistent_dir(tmp_path: Path) -> None:
    assert _discover_skills(tmp_path / "nope") == {}


def test_discover_ignores_plain_files(tmp_path: Path) -> None:
    base = tmp_path / "skills"
    base.mkdir()
    (base / "not-a-skill.txt").write_text("just a file")
    _make_skill(base, "real-skill")
    result = _discover_skills(base)
    assert list(result.keys()) == ["real-skill"]


# ---------------------------------------------------------------------------
# Group 2: _resolve_canonical
# ---------------------------------------------------------------------------


def test_canonical_ductor_priority(tmp_path: Path) -> None:
    ductor = {"sk": tmp_path / "ductor" / "sk"}
    claude = {"sk": tmp_path / "claude" / "sk"}
    codex = {"sk": tmp_path / "codex" / "sk"}
    for d in (ductor["sk"], claude["sk"], codex["sk"]):
        d.mkdir(parents=True)
    result = _resolve_canonical("sk", ductor, claude, codex)
    assert result == ductor["sk"]


def test_canonical_claude_over_codex(tmp_path: Path) -> None:
    claude = {"sk": tmp_path / "claude" / "sk"}
    codex = {"sk": tmp_path / "codex" / "sk"}
    claude["sk"].mkdir(parents=True)
    codex["sk"].mkdir(parents=True)
    result = _resolve_canonical("sk", {}, claude, codex)
    assert result == claude["sk"]


def test_canonical_only_codex(tmp_path: Path) -> None:
    codex = {"sk": tmp_path / "codex" / "sk"}
    codex["sk"].mkdir(parents=True)
    result = _resolve_canonical("sk", {}, {}, codex)
    assert result == codex["sk"]


def test_canonical_follows_symlink(tmp_path: Path) -> None:
    real = tmp_path / "external" / "sk"
    real.mkdir(parents=True)
    link = tmp_path / "claude" / "sk"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    result = _resolve_canonical("sk", {}, {"sk": link}, {})
    assert result == real.resolve()


def test_canonical_none_for_missing() -> None:
    result = _resolve_canonical("sk", {}, {}, {})
    assert result is None


# ---------------------------------------------------------------------------
# Group 3: _ensure_link
# ---------------------------------------------------------------------------


def test_create_link_new(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    assert _ensure_link(link, target) is True
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_ensure_link_already_correct(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    assert _ensure_link(link, target) is False


def test_ensure_link_wrong_target(tmp_path: Path) -> None:
    old_target = tmp_path / "old"
    new_target = tmp_path / "new"
    old_target.mkdir()
    new_target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(old_target)
    assert _ensure_link(link, new_target) is True
    assert link.resolve() == new_target.resolve()


def test_ensure_link_preserves_real_dir(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    assert _ensure_link(real_dir, target) is False
    assert not real_dir.is_symlink()


# ---------------------------------------------------------------------------
# Group 4: _clean_broken_links
# ---------------------------------------------------------------------------


def test_clean_removes_broken(tmp_path: Path) -> None:
    d = tmp_path / "skills"
    d.mkdir()
    (d / "broken").symlink_to(tmp_path / "gone")
    assert _clean_broken_links(d) == 1
    assert not (d / "broken").exists()


def test_clean_preserves_valid(tmp_path: Path) -> None:
    d = tmp_path / "skills"
    d.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    (d / "good").symlink_to(real)
    assert _clean_broken_links(d) == 0
    assert (d / "good").is_symlink()


def test_clean_nonexistent_dir(tmp_path: Path) -> None:
    assert _clean_broken_links(tmp_path / "nope") == 0


# ---------------------------------------------------------------------------
# Group 5: sync_skills (full integration)
# ---------------------------------------------------------------------------


def _setup_three_dirs(
    tmp_path: Path,
) -> tuple[DuctorPaths, Path, Path]:
    paths = _make_paths(tmp_path)
    paths.skills_dir.mkdir(parents=True)
    claude_home = tmp_path / "fake_home" / ".claude"
    codex_home = tmp_path / "fake_home" / ".codex"
    claude_home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    return paths, claude_home / "skills", codex_home / "skills"


def test_sync_claude_to_ductor(tmp_path: Path) -> None:
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    _make_skill(claude_skills, "from-claude")
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        sync_skills(paths)
    link = paths.skills_dir / "from-claude"
    assert link.is_symlink()
    assert link.resolve() == (claude_skills / "from-claude").resolve()


def test_sync_codex_to_ductor(tmp_path: Path) -> None:
    paths, _, codex_skills = _setup_three_dirs(tmp_path)
    _make_skill(codex_skills, "from-codex")
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"codex": codex_skills}
        sync_skills(paths)
    link = paths.skills_dir / "from-codex"
    assert link.is_symlink()
    assert link.resolve() == (codex_skills / "from-codex").resolve()


def test_sync_ductor_to_both(tmp_path: Path) -> None:
    paths, claude_skills, codex_skills = _setup_three_dirs(tmp_path)
    claude_skills.mkdir(parents=True, exist_ok=True)
    codex_skills.mkdir(parents=True, exist_ok=True)
    _make_skill(paths.skills_dir, "from-ductor")
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills, "codex": codex_skills}
        sync_skills(paths)
    for d in (claude_skills, codex_skills):
        link = d / "from-ductor"
        assert link.is_symlink()
        assert link.resolve() == (paths.skills_dir / "from-ductor").resolve()


def test_sync_no_providers(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.skills_dir.mkdir(parents=True)
    _make_skill(paths.skills_dir, "lonely")
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {}
        sync_skills(paths)
    assert (paths.skills_dir / "lonely").is_dir()


def test_sync_preserves_real_dirs(tmp_path: Path) -> None:
    paths, claude_skills, codex_skills = _setup_three_dirs(tmp_path)
    _make_skill(claude_skills, "shared")
    _make_skill(codex_skills, "shared")
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills, "codex": codex_skills}
        sync_skills(paths)
    assert not (claude_skills / "shared").is_symlink()
    assert not (codex_skills / "shared").is_symlink()
    link = paths.skills_dir / "shared"
    assert link.is_symlink()


def test_sync_external_symlink(tmp_path: Path) -> None:
    paths, claude_skills, codex_skills = _setup_three_dirs(tmp_path)
    codex_skills.mkdir(parents=True, exist_ok=True)
    external_real = tmp_path / "agents" / "skills" / "ext-skill"
    external_real.mkdir(parents=True)
    (external_real / "SKILL.md").write_text("# external")
    (claude_skills.parent).mkdir(parents=True, exist_ok=True)
    claude_skills.mkdir(exist_ok=True)
    (claude_skills / "ext-skill").symlink_to(external_real)
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills, "codex": codex_skills}
        sync_skills(paths)
    ductor_link = paths.skills_dir / "ext-skill"
    assert ductor_link.is_symlink()
    assert ductor_link.resolve() == external_real.resolve()


def test_sync_idempotent(tmp_path: Path) -> None:
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    claude_skills.mkdir(parents=True, exist_ok=True)
    _make_skill(paths.skills_dir, "my-skill")
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        sync_skills(paths)
        sync_skills(paths)
    link = claude_skills / "my-skill"
    assert link.is_symlink()
    assert link.resolve() == (paths.skills_dir / "my-skill").resolve()


def test_sync_cleans_broken_after_delete(tmp_path: Path) -> None:
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    claude_skills.mkdir(parents=True, exist_ok=True)
    sk = _make_skill(paths.skills_dir, "temp-skill")
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        sync_skills(paths)
    assert (claude_skills / "temp-skill").is_symlink()
    shutil.rmtree(sk)
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        sync_skills(paths)
    assert not (claude_skills / "temp-skill").exists()


# ---------------------------------------------------------------------------
# Group 6: watch_skill_sync (async watcher)
# ---------------------------------------------------------------------------


async def test_watch_detects_new_skill(tmp_path: Path) -> None:
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    claude_skills.mkdir(parents=True, exist_ok=True)
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        task = asyncio.create_task(watch_skill_sync(paths, interval=0.1))
        try:
            _make_skill(claude_skills, "new-skill")
            await asyncio.sleep(0.4)
            assert (paths.skills_dir / "new-skill").is_symlink()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_watch_cancellation(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.skills_dir.mkdir(parents=True)
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {}
        task = asyncio.create_task(watch_skill_sync(paths, interval=0.1))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Group 7: DuctorPaths.skills_dir property
# ---------------------------------------------------------------------------


def test_skills_dir_property(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    assert paths.skills_dir == paths.workspace / "skills"


# ---------------------------------------------------------------------------
# Group 8: Edge cases
# ---------------------------------------------------------------------------


def test_deeply_nested_skill(tmp_path: Path) -> None:
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    sk = claude_skills / "complex-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("# complex")
    (sk / "scripts").mkdir()
    (sk / "scripts" / "run.py").write_text("print('hello')")
    (sk / "results").mkdir()
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        sync_skills(paths)
    link = paths.skills_dir / "complex-skill"
    assert link.is_symlink()
    assert (link / "scripts" / "run.py").read_text() == "print('hello')"


@pytest.mark.skipif(
    not hasattr(Path, "symlink_to"),
    reason="Platform does not support symlinks",
)
def test_permission_error_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    _make_skill(paths.skills_dir, "fail-skill")
    with (
        patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock_dirs,
        patch("ductor_bot.workspace.skill_sync._create_dir_link", side_effect=OSError("denied")),
    ):
        mock_dirs.return_value = {"claude": claude_skills}
        sync_skills(paths)
    assert "denied" in caplog.text
