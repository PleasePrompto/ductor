"""Tests for workspace initialization."""

from __future__ import annotations

import json
from pathlib import Path

from ductor_bot.workspace.init import init_workspace, inject_runtime_environment
from ductor_bot.workspace.paths import DuctorPaths


def _setup_home_defaults(fw_root: Path) -> None:
    """Create a minimal home-defaults template for testing.

    Mirrors the repo ``workspace/`` structure (1:1 copy of ~/.ductor/).
    """
    ws = fw_root / "workspace"

    # Top-level CLAUDE.md (ductor home)
    ws.mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("# Ductor Home CLAUDE.md")

    config_dir = ws / "config"
    config_dir.mkdir()

    # workspace/CLAUDE.md (main agent rules)
    inner = ws / "workspace"
    inner.mkdir()
    (inner / "CLAUDE.md").write_text("# Framework CLAUDE.md")

    # Subdirectory CLAUDE.md files
    for subdir in ("memory_system", "cron_tasks", "output_to_user", "telegram_files"):
        d = inner / subdir
        d.mkdir()
        (d / "CLAUDE.md").write_text(f"# {subdir} CLAUDE.md")

    # MAINMEMORY.md (seed-only)
    (inner / "memory_system" / "MAINMEMORY.md").write_text("# Main Memory\n")

    # tools/ tree
    tools = inner / "tools"
    tools.mkdir()
    (tools / "CLAUDE.md").write_text("# Tools CLAUDE.md")
    cron_dir = tools / "cron_tools"
    cron_dir.mkdir()
    (cron_dir / "CLAUDE.md").write_text("# Cron Tools CLAUDE.md")
    (cron_dir / "cron_list.py").write_text("# cron_list stub")
    user_dir = tools / "user_tools"
    user_dir.mkdir()
    (user_dir / "CLAUDE.md").write_text("# User Tools CLAUDE.md")

    # config.example.json at framework root (for smart-merge)
    (fw_root / "config.example.json").write_text('{"provider": "claude", "model": "opus"}')


def _make_paths(tmp_path: Path) -> DuctorPaths:
    fw_root = tmp_path / "framework"
    _setup_home_defaults(fw_root)
    return DuctorPaths(
        ductor_home=tmp_path / "ductor_home",
        home_defaults=fw_root / "workspace",
        framework_root=fw_root,
    )


# -- directory creation --


def test_creates_workspace_dir(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    assert paths.workspace.is_dir()


def test_creates_config_dir(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    assert paths.config_dir.is_dir()


def test_creates_logs_dir(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    assert paths.logs_dir.is_dir()


def test_creates_cron_tasks_dir(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    assert paths.cron_tasks_dir.is_dir()


def test_creates_tools_dir(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    assert paths.tools_dir.is_dir()


# -- Zone 2: framework files always overwritten --


def test_copies_claude_md(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    target = paths.workspace / "CLAUDE.md"
    assert target.exists()
    assert not target.is_symlink()
    assert target.read_text() == "# Framework CLAUDE.md"


def test_copies_agents_md_mirrors_claude_md(tmp_path: Path) -> None:
    """AGENTS.md (Codex rule file) is a copy of CLAUDE.md, not a separate file."""
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    claude = paths.workspace / "CLAUDE.md"
    agents = paths.workspace / "AGENTS.md"
    assert agents.exists()
    assert not agents.is_symlink()
    assert agents.read_text() == claude.read_text()


def test_framework_files_updated_on_reinit(tmp_path: Path) -> None:
    """Framework files are overwritten on every init (not user-owned)."""
    paths = _make_paths(tmp_path)
    init_workspace(paths)

    # Simulate framework update
    inner_claude = paths.home_defaults / "workspace" / "CLAUDE.md"
    inner_claude.write_text("# Updated CLAUDE.md")
    init_workspace(paths)

    assert (paths.workspace / "CLAUDE.md").read_text() == "# Updated CLAUDE.md"


def test_subdirectory_claude_md_updated_on_reinit(tmp_path: Path) -> None:
    """Subdirectory CLAUDE.md files (Zone 2) are overwritten on every init."""
    paths = _make_paths(tmp_path)
    init_workspace(paths)

    # User modifies a subdirectory CLAUDE.md (should be overwritten)
    mem_claude = paths.memory_system_dir / "CLAUDE.md"
    mem_claude.write_text("# User modification")

    # Simulate framework update to the template
    template = paths.home_defaults / "workspace" / "memory_system" / "CLAUDE.md"
    template.write_text("# Updated memory_system CLAUDE.md")

    init_workspace(paths)
    assert mem_claude.read_text() == "# Updated memory_system CLAUDE.md"


def test_subdirectory_agents_md_created_from_claude_md(tmp_path: Path) -> None:
    """AGENTS.md is auto-created for every CLAUDE.md in subdirectories."""
    paths = _make_paths(tmp_path)
    init_workspace(paths)

    for subdir in ("memory_system", "cron_tasks", "output_to_user", "telegram_files"):
        agents = paths.workspace / subdir / "AGENTS.md"
        claude = paths.workspace / subdir / "CLAUDE.md"
        assert agents.exists(), f"AGENTS.md missing in {subdir}"
        assert agents.read_text() == claude.read_text()


def test_replaces_stale_symlinks_with_copies(tmp_path: Path) -> None:
    """Old symlinks from previous versions get replaced with real files."""
    paths = _make_paths(tmp_path)
    paths.workspace.mkdir(parents=True)

    # Simulate old symlink
    link = paths.workspace / "CLAUDE.md"
    src = paths.home_defaults / "workspace" / "CLAUDE.md"
    link.symlink_to(src)
    assert link.is_symlink()

    init_workspace(paths)
    assert not link.is_symlink()
    assert link.read_text() == "# Framework CLAUDE.md"


# -- Zone 3: seed defaults (never overwrite) --


def test_seeds_mainmemory(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    assert paths.mainmemory_path.exists()
    assert len(paths.mainmemory_path.read_text()) > 0


def test_seeds_tools_claude_md(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    tools_claude = paths.tools_dir / "CLAUDE.md"
    assert tools_claude.exists()
    assert tools_claude.read_text() == "# Tools CLAUDE.md"


def test_seeds_tools_agents_md_mirrors_claude_md(tmp_path: Path) -> None:
    """tools/AGENTS.md is mirrored from tools/CLAUDE.md, not a separate file."""
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    tools_claude = paths.tools_dir / "CLAUDE.md"
    tools_agents = paths.tools_dir / "AGENTS.md"
    assert tools_agents.exists()
    assert tools_agents.read_text() == tools_claude.read_text()


# -- Zone 3: never overwrite user files --


def test_does_not_overwrite_mainmemory(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.memory_system_dir.mkdir(parents=True)
    paths.mainmemory_path.write_text("My custom memories")

    init_workspace(paths)
    assert paths.mainmemory_path.read_text() == "My custom memories"


def test_seeds_tool_subdirectories(tmp_path: Path) -> None:
    """Tool subdirectories (cron_tools/, etc.) are recursively seeded."""
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    cron_list = paths.tools_dir / "cron_tools" / "cron_list.py"
    assert cron_list.exists()
    assert cron_list.read_text() == "# cron_list stub"


def test_does_not_overwrite_user_tool_scripts(tmp_path: Path) -> None:
    """User-modified tool scripts are not overwritten."""
    paths = _make_paths(tmp_path)
    cron_dir = paths.tools_dir / "cron_tools"
    cron_dir.mkdir(parents=True)
    (cron_dir / "cron_list.py").write_text("# my custom version")

    init_workspace(paths)
    assert (cron_dir / "cron_list.py").read_text() == "# my custom version"


def test_ductor_home_claude_md_overwritten(tmp_path: Path) -> None:
    """The ductor_home/CLAUDE.md is Zone 2 (always overwritten)."""
    paths = _make_paths(tmp_path)
    init_workspace(paths)

    home_claude = paths.ductor_home / "CLAUDE.md"
    assert home_claude.exists()
    assert home_claude.read_text() == "# Ductor Home CLAUDE.md"

    # Simulate user editing it -- should be overwritten on reinit
    home_claude.write_text("# User edit")
    init_workspace(paths)
    assert home_claude.read_text() == "# Ductor Home CLAUDE.md"


# -- config smart-merge --


def test_creates_config_from_example(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    assert paths.config_path.exists()
    config = json.loads(paths.config_path.read_text())
    assert config["provider"] == "claude"


def test_config_merge_adds_new_keys(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.config_path.write_text('{"provider": "codex"}')

    init_workspace(paths)
    config = json.loads(paths.config_path.read_text())
    assert config["provider"] == "codex"  # User value preserved
    assert config["model"] == "opus"  # New key added from defaults


def test_config_merge_preserves_user_values(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.config_path.write_text('{"provider": "codex", "model": "sonnet"}')

    init_workspace(paths)
    config = json.loads(paths.config_path.read_text())
    assert config["provider"] == "codex"
    assert config["model"] == "sonnet"


# -- idempotency --


def test_idempotent_double_init(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    init_workspace(paths)  # Second call should not fail

    assert paths.workspace.is_dir()
    assert (paths.workspace / "CLAUDE.md").exists()
    assert paths.mainmemory_path.exists()


# -- orphan symlink cleanup --


def test_cleans_orphan_symlinks(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)

    # Create a dangling symlink
    orphan = paths.workspace / "ORPHAN.md"
    orphan.symlink_to(tmp_path / "nonexistent.md")
    assert orphan.is_symlink()

    init_workspace(paths)
    assert not orphan.exists()


# -- runtime environment injection --


def test_inject_docker_notice(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    inject_runtime_environment(paths, docker_container="ductor-sandbox")
    content = (paths.workspace / "CLAUDE.md").read_text()
    assert "DOCKER CONTAINER" in content
    assert "ductor-sandbox" in content
    # AGENTS.md mirror should also have it
    agents = (paths.workspace / "AGENTS.md").read_text()
    assert "DOCKER CONTAINER" in agents


def test_inject_host_notice(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    inject_runtime_environment(paths, docker_container="")
    content = (paths.workspace / "CLAUDE.md").read_text()
    assert "HOST SYSTEM" in content
    assert "NO SANDBOX" in content


def test_inject_no_duplicate(tmp_path: Path) -> None:
    """Calling inject twice does not duplicate the section."""
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    inject_runtime_environment(paths, docker_container="ctr-1")
    inject_runtime_environment(paths, docker_container="ctr-1")
    content = (paths.workspace / "CLAUDE.md").read_text()
    assert content.count("## Runtime Environment") == 1


def test_inject_refreshed_on_reinit(tmp_path: Path) -> None:
    """Workspace re-init overwrites Zone 2, then inject writes fresh notice."""
    paths = _make_paths(tmp_path)
    init_workspace(paths)
    inject_runtime_environment(paths, docker_container="ctr-old")
    # Re-init overwrites CLAUDE.md (Zone 2), removing the old notice
    init_workspace(paths)
    content = (paths.workspace / "CLAUDE.md").read_text()
    assert "## Runtime Environment" not in content
    # Fresh inject
    inject_runtime_environment(paths, docker_container="ctr-new")
    content = (paths.workspace / "CLAUDE.md").read_text()
    assert "ctr-new" in content
