"""Tests for cross-platform skill directory sync."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from ductor_bot.workspace.paths import DuctorPaths
from ductor_bot.workspace.skill_sync import (
    _MANAGED_MARKER,
    _SYNCABLE_PROVIDERS,
    _clean_broken_links,
    _clean_invalid_workspace_skill_links,
    _cli_skill_dirs,
    _discover_skills,
    _ensure_copy,
    _ensure_link,
    _has_valid_skill_frontmatter,
    _is_managed_copy,
    _is_under,
    _load_skill_sync_config,
    _resolve_canonical,
    cleanup_ductor_links,
    sync_bundled_skills,
    sync_skills,
    watch_skill_sync,
)


def _make_paths(tmp_path: Path) -> DuctorPaths:
    return DuctorPaths(
        ductor_home=tmp_path / "ductor_home",
        home_defaults=tmp_path / "fw" / "_home_defaults",
        framework_root=tmp_path / "fw",
    )


def _make_skill(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_text(name))
    return d


def _skill_text(name: str, body: str | None = None) -> str:
    return f"---\nname: {name}\ndescription: Test skill {name}.\n---\n\n{body or f'# {name}'}"


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


def test_discover_skips_missing_frontmatter(tmp_path: Path) -> None:
    base = tmp_path / "skills"
    invalid = base / "invalid-skill"
    invalid.mkdir(parents=True)
    (invalid / "SKILL.md").write_text("# invalid")
    _make_skill(base, "valid-skill")
    result = _discover_skills(base)
    assert set(result.keys()) == {"valid-skill"}


def test_valid_skill_frontmatter_requires_name_and_description(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path, "valid")
    assert _has_valid_skill_frontmatter(skill) is True
    (skill / "SKILL.md").write_text("---\nname: valid\n---\n\n# Missing description")
    assert _has_valid_skill_frontmatter(skill) is False


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


def test_canonical_gemini_last_priority(tmp_path: Path) -> None:
    gemini = {"sk": tmp_path / "gemini" / "sk"}
    gemini["sk"].mkdir(parents=True)
    result = _resolve_canonical("sk", {}, {}, {}, gemini)
    assert result == gemini["sk"]


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


def test_sync_gemini_to_ductor(tmp_path: Path) -> None:
    paths, _, _ = _setup_three_dirs(tmp_path)
    gemini_home = tmp_path / "fake_home" / ".gemini"
    gemini_home.mkdir(parents=True)
    gemini_skills = gemini_home / "skills"
    _make_skill(gemini_skills, "from-gemini")
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"gemini": gemini_skills}
        sync_skills(paths)
    link = paths.skills_dir / "from-gemini"
    assert link.is_symlink()
    assert link.resolve() == (gemini_skills / "from-gemini").resolve()


def test_sync_ductor_to_all_three(tmp_path: Path) -> None:
    paths, claude_skills, codex_skills = _setup_three_dirs(tmp_path)
    gemini_home = tmp_path / "fake_home" / ".gemini"
    gemini_home.mkdir(parents=True)
    gemini_skills = gemini_home / "skills"
    claude_skills.mkdir(parents=True, exist_ok=True)
    codex_skills.mkdir(parents=True, exist_ok=True)
    gemini_skills.mkdir(parents=True, exist_ok=True)
    _make_skill(paths.skills_dir, "from-ductor")
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {
            "claude": claude_skills,
            "codex": codex_skills,
            "gemini": gemini_skills,
        }
        sync_skills(paths)
    for d in (claude_skills, codex_skills, gemini_skills):
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
    (external_real / "SKILL.md").write_text(_skill_text("ext-skill", "# external"))
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


def test_sync_removes_invalid_workspace_symlink_and_downstream_link(tmp_path: Path) -> None:
    paths, _, codex_skills = _setup_three_dirs(tmp_path)
    codex_skills.mkdir(parents=True, exist_ok=True)
    external = tmp_path / "external" / "sisyphus-orchestrator"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("# missing frontmatter")
    workspace_link = paths.skills_dir / "sisyphus-orchestrator"
    workspace_link.symlink_to(external)
    (codex_skills / "sisyphus-orchestrator").symlink_to(workspace_link)

    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"codex": codex_skills}
        sync_skills(paths)

    assert not workspace_link.exists()
    assert not (codex_skills / "sisyphus-orchestrator").exists()


def test_clean_invalid_workspace_skills_preserves_real_user_dir(tmp_path: Path) -> None:
    base = tmp_path / "skills"
    user_skill = base / "user-skill"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("# user-managed invalid skill")

    assert _clean_invalid_workspace_skill_links(base) == 0
    assert user_skill.is_dir()


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
    (sk / "SKILL.md").write_text(_skill_text("complex-skill", "# complex"))
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


# ---------------------------------------------------------------------------
# Group 9: _is_under helper
# ---------------------------------------------------------------------------


def test_is_under_child(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child = parent / "sub" / "deep"
    parent.mkdir()
    child.mkdir(parents=True)
    assert _is_under(child, parent) is True


def test_is_under_same(tmp_path: Path) -> None:
    d = tmp_path / "dir"
    d.mkdir()
    assert _is_under(d, d) is True


def test_is_under_unrelated(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert _is_under(a, b) is False


# ---------------------------------------------------------------------------
# Group 10: External symlink protection in sync
# ---------------------------------------------------------------------------


def test_sync_preserves_external_symlink(tmp_path: Path) -> None:
    """User has a symlink in .claude pointing outside sync dirs -- must not be replaced."""
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    claude_skills.mkdir(parents=True, exist_ok=True)

    # User's external skill (outside all sync dirs)
    external = tmp_path / "my_custom" / "my-skill"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text(_skill_text("my-skill", "# user version"))

    # User symlink in .claude pointing to external location
    (claude_skills / "my-skill").symlink_to(external)

    # Ductor also has a skill with the same name
    _make_skill(paths.skills_dir, "my-skill")

    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        sync_skills(paths)

    # User's external symlink must be preserved
    link = claude_skills / "my-skill"
    assert link.is_symlink()
    assert link.resolve() == external.resolve()


def test_sync_replaces_internal_symlink(tmp_path: Path) -> None:
    """Symlinks pointing inside sync dirs (ductor-managed) can be updated."""
    paths, claude_skills, codex_skills = _setup_three_dirs(tmp_path)
    claude_skills.mkdir(parents=True, exist_ok=True)
    codex_skills.mkdir(parents=True, exist_ok=True)

    # Codex has a real skill
    _make_skill(codex_skills, "shared")

    # Claude has a symlink pointing to codex (inside sync dirs)
    (claude_skills / "shared").symlink_to(codex_skills / "shared")

    # Ductor now has a real skill with the same name (higher priority)
    _make_skill(paths.skills_dir, "shared")

    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills, "codex": codex_skills}
        sync_skills(paths)

    # Claude's symlink should now point to ductor (higher priority canonical)
    link = claude_skills / "shared"
    assert link.is_symlink()
    assert link.resolve() == (paths.skills_dir / "shared").resolve()


# ---------------------------------------------------------------------------
# Group 11: sync_bundled_skills
# ---------------------------------------------------------------------------


def test_bundled_creates_symlink(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    bundled = paths.bundled_skills_dir
    _make_skill(bundled, "default-skill")
    paths.skills_dir.mkdir(parents=True)

    sync_bundled_skills(paths)

    target = paths.skills_dir / "default-skill"
    assert target.is_symlink()
    assert target.resolve() == (bundled / "default-skill").resolve()


def test_bundled_preserves_user_real_dir(tmp_path: Path) -> None:
    """User has a real directory with the same name as a bundled skill."""
    paths = _make_paths(tmp_path)
    _make_skill(paths.bundled_skills_dir, "my-skill")

    # User already has their own version
    user_skill = _make_skill(paths.skills_dir, "my-skill")
    (user_skill / "SKILL.md").write_text("# user custom version")

    sync_bundled_skills(paths)

    target = paths.skills_dir / "my-skill"
    assert not target.is_symlink()
    assert (target / "SKILL.md").read_text() == "# user custom version"


def test_bundled_updates_stale_symlink(tmp_path: Path) -> None:
    """Existing symlink pointing to wrong bundled location gets updated."""
    paths = _make_paths(tmp_path)
    _make_skill(paths.bundled_skills_dir, "default-skill")
    paths.skills_dir.mkdir(parents=True)

    old_target = tmp_path / "old_pkg" / "default-skill"
    old_target.mkdir(parents=True)
    (paths.skills_dir / "default-skill").symlink_to(old_target)

    sync_bundled_skills(paths)

    target = paths.skills_dir / "default-skill"
    assert target.is_symlink()
    assert target.resolve() == (paths.bundled_skills_dir / "default-skill").resolve()


def test_bundled_idempotent(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    _make_skill(paths.bundled_skills_dir, "default-skill")
    paths.skills_dir.mkdir(parents=True)

    sync_bundled_skills(paths)
    sync_bundled_skills(paths)

    target = paths.skills_dir / "default-skill"
    assert target.is_symlink()
    assert target.resolve() == (paths.bundled_skills_dir / "default-skill").resolve()


def test_bundled_skips_files(tmp_path: Path) -> None:
    """Plain files in bundled dir (like CLAUDE.md) are ignored."""
    paths = _make_paths(tmp_path)
    bundled = paths.bundled_skills_dir
    bundled.mkdir(parents=True)
    (bundled / "CLAUDE.md").write_text("# docs")
    paths.skills_dir.mkdir(parents=True)

    sync_bundled_skills(paths)

    assert not (paths.skills_dir / "CLAUDE.md").is_symlink()


def test_bundled_no_dir_noop(tmp_path: Path) -> None:
    """No crash when bundled_skills_dir does not exist."""
    paths = _make_paths(tmp_path)
    sync_bundled_skills(paths)


# ---------------------------------------------------------------------------
# Group 12: cleanup_ductor_links
# ---------------------------------------------------------------------------


def test_cleanup_removes_ductor_links(tmp_path: Path) -> None:
    paths, claude_skills, codex_skills = _setup_three_dirs(tmp_path)
    claude_skills.mkdir(parents=True, exist_ok=True)
    codex_skills.mkdir(parents=True, exist_ok=True)

    # Simulate ductor-created symlinks (target under ductor skills dir)
    _make_skill(paths.skills_dir, "from-ductor")
    (claude_skills / "from-ductor").symlink_to(paths.skills_dir / "from-ductor")
    (codex_skills / "from-ductor").symlink_to(paths.skills_dir / "from-ductor")

    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills, "codex": codex_skills}
        removed = cleanup_ductor_links(paths)

    assert removed == 2
    assert not (claude_skills / "from-ductor").exists()
    assert not (codex_skills / "from-ductor").exists()


def test_cleanup_preserves_external_links(tmp_path: Path) -> None:
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    claude_skills.mkdir(parents=True, exist_ok=True)

    external = tmp_path / "external" / "user-skill"
    external.mkdir(parents=True)
    (claude_skills / "user-skill").symlink_to(external)

    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        removed = cleanup_ductor_links(paths)

    assert removed == 0
    assert (claude_skills / "user-skill").is_symlink()


def test_cleanup_preserves_real_dirs(tmp_path: Path) -> None:
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    _make_skill(claude_skills, "real-skill")

    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        removed = cleanup_ductor_links(paths)

    assert removed == 0
    assert (claude_skills / "real-skill").is_dir()
    assert not (claude_skills / "real-skill").is_symlink()


def test_cleanup_removes_bundled_links(tmp_path: Path) -> None:
    """Symlinks pointing to bundled skills dir are also cleaned up."""
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    claude_skills.mkdir(parents=True, exist_ok=True)

    bundled_skill = _make_skill(paths.bundled_skills_dir, "bundled-one")
    (claude_skills / "bundled-one").symlink_to(bundled_skill)

    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        removed = cleanup_ductor_links(paths)

    assert removed == 1
    assert not (claude_skills / "bundled-one").exists()


def test_cleanup_no_providers(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {}
        removed = cleanup_ductor_links(paths)
    assert removed == 0


# ---------------------------------------------------------------------------
# Group 13: bundled_skills_dir property
# ---------------------------------------------------------------------------


def test_bundled_skills_dir_property(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    assert paths.bundled_skills_dir == paths.home_defaults / "workspace" / "skills"


# ---------------------------------------------------------------------------
# Group 14: Docker copy mode -- _is_managed_copy / _ensure_copy
# ---------------------------------------------------------------------------


def test_is_managed_copy_false_for_normal_dir(tmp_path: Path) -> None:
    d = tmp_path / "skill"
    d.mkdir()
    assert _is_managed_copy(d) is False


def test_is_managed_copy_false_for_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert _is_managed_copy(link) is False


def test_is_managed_copy_true_with_marker(tmp_path: Path) -> None:
    d = tmp_path / "skill"
    d.mkdir()
    (d / _MANAGED_MARKER).touch()
    assert _is_managed_copy(d) is True


def test_ensure_copy_creates_new(tmp_path: Path) -> None:
    source = _make_skill(tmp_path / "src", "my-skill")
    dest = tmp_path / "dest" / "my-skill"
    dest.parent.mkdir(parents=True)
    assert _ensure_copy(dest, source) is True
    assert dest.is_dir()
    assert not dest.is_symlink()
    assert (dest / "SKILL.md").read_text() == _skill_text("my-skill")
    assert (dest / _MANAGED_MARKER).is_file()


def test_ensure_copy_skips_user_dir(tmp_path: Path) -> None:
    source = _make_skill(tmp_path / "src", "sk")
    dest = _make_skill(tmp_path / "dest", "sk")
    (dest / "SKILL.md").write_text("# user version")
    assert _ensure_copy(dest, source) is False
    assert (dest / "SKILL.md").read_text() == "# user version"


def test_ensure_copy_replaces_symlink(tmp_path: Path) -> None:
    source = _make_skill(tmp_path / "src", "sk")
    real = tmp_path / "old"
    real.mkdir()
    dest = tmp_path / "dest" / "sk"
    dest.parent.mkdir(parents=True)
    dest.symlink_to(real)
    assert _ensure_copy(dest, source) is True
    assert not dest.is_symlink()
    assert (dest / _MANAGED_MARKER).is_file()


def test_ensure_copy_updates_stale(tmp_path: Path) -> None:
    import time

    source = _make_skill(tmp_path / "src", "sk")
    dest = tmp_path / "dest" / "sk"
    dest.parent.mkdir(parents=True)
    _ensure_copy(dest, source)
    marker_mtime = (dest / _MANAGED_MARKER).stat().st_mtime

    time.sleep(0.05)
    (source / "SKILL.md").write_text("# updated")

    assert _ensure_copy(dest, source) is True
    assert (dest / "SKILL.md").read_text() == "# updated"
    assert (dest / _MANAGED_MARKER).stat().st_mtime > marker_mtime


def test_ensure_copy_idempotent(tmp_path: Path) -> None:
    source = _make_skill(tmp_path / "src", "sk")
    dest = tmp_path / "dest" / "sk"
    dest.parent.mkdir(parents=True)
    _ensure_copy(dest, source)
    assert _ensure_copy(dest, source) is False


# ---------------------------------------------------------------------------
# Group 15: Docker copy mode -- sync_skills / sync_bundled_skills
# ---------------------------------------------------------------------------


def test_sync_docker_copies_instead_of_links(tmp_path: Path) -> None:
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    claude_skills.mkdir(parents=True, exist_ok=True)
    _make_skill(paths.skills_dir, "my-skill")
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        sync_skills(paths, docker_active=True)
    dest = claude_skills / "my-skill"
    assert dest.is_dir()
    assert not dest.is_symlink()
    assert _is_managed_copy(dest)
    assert (dest / "SKILL.md").read_text() == _skill_text("my-skill")


def test_sync_docker_preserves_user_dirs(tmp_path: Path) -> None:
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    _make_skill(claude_skills, "shared")
    _make_skill(paths.skills_dir, "shared")
    (claude_skills / "shared" / "SKILL.md").write_text("# user version")
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        sync_skills(paths, docker_active=True)
    assert not (claude_skills / "shared").is_symlink()
    assert (claude_skills / "shared" / "SKILL.md").read_text() == "# user version"


def test_sync_docker_idempotent(tmp_path: Path) -> None:
    paths, claude_skills, _ = _setup_three_dirs(tmp_path)
    claude_skills.mkdir(parents=True, exist_ok=True)
    _make_skill(paths.skills_dir, "sk")
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock:
        mock.return_value = {"claude": claude_skills}
        sync_skills(paths, docker_active=True)
        sync_skills(paths, docker_active=True)
    assert _is_managed_copy(claude_skills / "sk")


def test_bundled_docker_copies(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    _make_skill(paths.bundled_skills_dir, "bundled-sk")
    paths.skills_dir.mkdir(parents=True)

    sync_bundled_skills(paths, docker_active=True)

    target = paths.skills_dir / "bundled-sk"
    assert target.is_dir()
    assert not target.is_symlink()
    assert _is_managed_copy(target)
    assert (target / "SKILL.md").read_text() == _skill_text("bundled-sk")


def test_bundled_docker_preserves_user_dir(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    _make_skill(paths.bundled_skills_dir, "sk")
    user_sk = _make_skill(paths.skills_dir, "sk")
    (user_sk / "SKILL.md").write_text("# custom")

    sync_bundled_skills(paths, docker_active=True)

    assert not (paths.skills_dir / "sk").is_symlink()
    assert (paths.skills_dir / "sk" / "SKILL.md").read_text() == "# custom"


def test_bundled_docker_replaces_old_symlink(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    _make_skill(paths.bundled_skills_dir, "sk")
    paths.skills_dir.mkdir(parents=True)
    old = tmp_path / "old"
    old.mkdir()
    (paths.skills_dir / "sk").symlink_to(old)

    sync_bundled_skills(paths, docker_active=True)

    target = paths.skills_dir / "sk"
    assert not target.is_symlink()
    assert _is_managed_copy(target)


# ---------------------------------------------------------------------------
# Group: skill sync toggle config (#141)
# ---------------------------------------------------------------------------

_ALL_PROVIDERS = _SYNCABLE_PROVIDERS  # the real constant — keeps this in sync with new providers


def test_load_skill_sync_config_missing_file(tmp_path: Path) -> None:
    enabled, providers = _load_skill_sync_config(tmp_path / "absent.json")
    assert enabled is True
    assert providers == _ALL_PROVIDERS


def test_load_skill_sync_config_without_block(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"provider": "claude"}))
    enabled, providers = _load_skill_sync_config(cfg)
    assert enabled is True
    assert providers == _ALL_PROVIDERS


def test_load_skill_sync_config_global_disable(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"skills": {"sync_enabled": False}}))
    enabled, _providers = _load_skill_sync_config(cfg)
    assert enabled is False


def test_load_skill_sync_config_per_provider_opt_out(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"skills": {"sync": {"codex": False}}}))
    enabled, providers = _load_skill_sync_config(cfg)
    assert enabled is True
    assert providers == _SYNCABLE_PROVIDERS - {"codex"}


def test_cli_skill_dirs_filters_disabled_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    for name in (".claude", ".codex", ".gemini"):
        (fake_home / name).mkdir(parents=True)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    with patch("ductor_bot.workspace.skill_sync.Path.home", return_value=fake_home):
        assert set(_cli_skill_dirs(None)) == {"claude", "codex", "gemini"}
        assert set(_cli_skill_dirs(frozenset({"claude", "gemini"}))) == {"claude", "gemini"}
        assert _cli_skill_dirs(frozenset()) == {}


def test_sync_skills_skipped_when_globally_disabled(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.skills_dir.mkdir(parents=True, exist_ok=True)
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(json.dumps({"skills": {"sync_enabled": False}}))
    with patch("ductor_bot.workspace.skill_sync._cli_skill_dirs") as mock_dirs:
        sync_skills(paths)
    mock_dirs.assert_not_called()
