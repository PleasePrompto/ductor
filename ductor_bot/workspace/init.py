"""Workspace initialization: walk home defaults, copy with zone rules, sync, merge."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

from ductor_bot.workspace.paths import DuctorPaths
from ductor_bot.workspace.rules_selector import RulesSelector
from ductor_bot.workspace.skill_sync import sync_bundled_skills, sync_skills

logger = logging.getLogger(__name__)

# Files that are ALWAYS overwritten on every start (Zone 2).
# Everything else is seeded only once (Zone 3).
_ZONE2_FILES = frozenset({"CLAUDE.md", "AGENTS.md"})

# Directories where ALL .py files are Zone 2 (framework-managed).
# User-owned scripts should go in tools/user_tools/ (Zone 3).
# Paths are relative to home_defaults root (include workspace/ prefix).
_ZONE2_PY_DIRS = frozenset({"workspace/tools/cron_tools", "workspace/tools/webhook_tools"})

# Rule templates are deployed separately by RulesSelector
_SKIP_FILES = frozenset(
    {
        "RULES-claude-only.md",
        "RULES-codex-only.md",
        "RULES-claude-and-codex.md",
        "RULES.md",  # Static templates also handled by RulesSelector
    }
)

_SKIP_DIRS = frozenset({".venv", ".git", ".mypy_cache", "__pycache__", "node_modules"})


# ---------------------------------------------------------------------------
# Home defaults sync (replaces _ensure_dirs + _copy_framework + _seed_defaults)
# ---------------------------------------------------------------------------


def _sync_home_defaults(paths: DuctorPaths) -> None:
    """Walk the home-defaults template and copy to ``ductor_home``.

    The template at ``<repo>/workspace/`` mirrors ``~/.ductor/`` exactly.
    Zone rules per file:

    - **Zone 2** (``_ZONE2_FILES``): always overwritten so framework updates
      reach users on restart.  ``CLAUDE.md`` also produces a matching
      ``AGENTS.md`` mirror automatically.
    - **Zone 3** (everything else): seeded on first run only, never
      overwritten so user modifications persist.
    """
    if not paths.home_defaults.is_dir():
        logger.warning("Home defaults directory not found: %s", paths.home_defaults)
        return
    _walk_and_copy(paths.home_defaults, paths.ductor_home)
    # Ensure logs dir exists (not in template because it holds no files)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)


def _should_skip_entry(entry: Path) -> bool:
    """Check if entry should be skipped during workspace sync."""
    return entry.name.startswith(".") or entry.name in _SKIP_DIRS or entry.name in _SKIP_FILES


def _is_zone2_py_file(entry: Path, src: Path, root_src: Path) -> bool:
    """Check if a .py file is in a Zone 2 directory (always overwritten)."""
    if entry.suffix != ".py":
        return False
    try:
        rel_dir = src.relative_to(root_src)
        return str(rel_dir) in _ZONE2_PY_DIRS
    except ValueError:
        return False


def _copy_with_symlink_check(entry: Path, target: Path) -> None:
    """Copy file to target, removing symlink if present."""
    if target.is_symlink():
        target.unlink()
    shutil.copy2(entry, target)


def _handle_zone2_file(entry: Path, target: Path, dst: Path) -> None:
    """Handle Zone 2 file copy (always overwrite) and AGENTS.md mirroring."""
    _copy_with_symlink_check(entry, target)
    logger.debug("Zone 2 copy: %s", target)
    # Auto-create AGENTS.md mirror for every CLAUDE.md
    if entry.name == "CLAUDE.md":
        agents_target = dst / "AGENTS.md"
        _copy_with_symlink_check(entry, agents_target)
        logger.debug("Zone 2 copy: %s", agents_target)


def _handle_regular_file(entry: Path, target: Path, src: Path, root_src: Path) -> None:
    """Handle regular file with Zone 2 .py or Zone 3 logic."""
    if _is_zone2_py_file(entry, src, root_src):
        # Zone 2 .py file: always overwrite (framework-controlled)
        _copy_with_symlink_check(entry, target)
        logger.debug("Zone 2 copy (framework tool): %s", target)
    elif not target.exists():
        # Zone 3: seed only (user-owned, never overwritten)
        shutil.copy2(entry, target)
        logger.debug("Zone 3 seed: %s", target)
    else:
        logger.debug("Zone 3 skip: %s (exists)", target)


def _walk_and_copy(src: Path, dst: Path, root_src: Path | None = None) -> None:
    """Recursively copy *src* tree into *dst* with zone-based overwrite rules.

    Args:
        src: Source directory to copy from
        dst: Destination directory to copy to
        root_src: Root source directory (for calculating relative paths). Defaults to src.
    """
    if root_src is None:
        root_src = src

    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if _should_skip_entry(entry):
            continue
        target = dst / entry.name
        if entry.is_dir():
            if target.is_symlink():
                logger.debug("Skip symlinked target: %s", target)
                continue
            _walk_and_copy(entry, target, root_src)
        elif entry.name in _ZONE2_FILES:
            _handle_zone2_file(entry, target, dst)
        else:
            _handle_regular_file(entry, target, src, root_src)


# ---------------------------------------------------------------------------
# Rule file sync (CLAUDE.md <-> AGENTS.md)
# ---------------------------------------------------------------------------


def sync_rule_files(root: Path) -> None:
    """Recursively sync CLAUDE.md <-> AGENTS.md by mtime in all subdirs.

    For each directory under root (including root itself):
    - If CLAUDE.md exists but AGENTS.md does not: copy CLAUDE.md to AGENTS.md
    - If AGENTS.md exists but CLAUDE.md does not: copy AGENTS.md to CLAUDE.md
    - If both exist: copy the newer one to the older one (by mtime)
    - Skip directories in _SKIP_DIRS
    """
    if not root.is_dir():
        return
    _sync_pair(root)
    for dirpath in root.rglob("*"):
        if not dirpath.is_dir():
            continue
        if any(part in _SKIP_DIRS for part in dirpath.parts):
            continue
        _sync_pair(dirpath)


def _sync_pair(directory: Path) -> None:
    """Sync CLAUDE.md and AGENTS.md in a single directory."""
    claude = directory / "CLAUDE.md"
    agents = directory / "AGENTS.md"

    if claude.exists() and not agents.exists():
        shutil.copy2(claude, agents)
    elif agents.exists() and not claude.exists():
        shutil.copy2(agents, claude)
    elif claude.exists() and agents.exists():
        claude_mtime = claude.stat().st_mtime
        agents_mtime = agents.stat().st_mtime
        if claude_mtime > agents_mtime:
            shutil.copy2(claude, agents)
        elif agents_mtime > claude_mtime:
            shutil.copy2(agents, claude)


# ---------------------------------------------------------------------------
# Config smart-merge
# ---------------------------------------------------------------------------


def _smart_merge_config(paths: DuctorPaths) -> None:
    """Create config from example or merge new keys into existing."""
    if not paths.config_example_path.exists():
        return

    try:
        defaults = json.loads(paths.config_example_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to parse config example: %s", paths.config_example_path)
        return

    if not paths.config_path.exists():
        paths.config_path.write_text(
            json.dumps(defaults, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return

    try:
        existing = json.loads(paths.config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to parse config: %s, skipping merge", paths.config_path)
        return
    merged = {**defaults, **existing}

    if merged != existing:
        paths.config_path.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------


def _migrate_tasks_to_cron_tasks(paths: DuctorPaths) -> None:
    """One-time migration: rename tasks/ to cron_tasks/ if needed."""
    old_tasks = paths.workspace / "tasks"
    if old_tasks.is_dir() and not paths.cron_tasks_dir.exists():
        old_tasks.rename(paths.cron_tasks_dir)
        logger.info("Migrated workspace/tasks/ -> workspace/cron_tasks/")


def _clean_orphan_symlinks(paths: DuctorPaths) -> None:
    """Remove broken symlinks in the workspace root."""
    if not paths.workspace.is_dir():
        return
    for entry in paths.workspace.iterdir():
        if entry.is_symlink() and not entry.exists():
            entry.unlink()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


_REQUIRED_DIRS = (
    "workspace",
    "workspace/memory_system",
    "workspace/cron_tasks",
    "workspace/tools",
    "workspace/tools/user_tools",
    "workspace/tools/cron_tools",
    "workspace/tools/telegram_tools",
    "workspace/tools/webhook_tools",
    "workspace/output_to_user",
    "workspace/telegram_files",
    "workspace/skills",
    "config",
    "logs",
)


def _ensure_required_dirs(paths: DuctorPaths) -> None:
    """Create any required directories that are missing."""
    for rel in _REQUIRED_DIRS:
        d = paths.ductor_home / rel
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            logger.info("Created missing directory: %s", d)


def init_workspace(paths: DuctorPaths) -> None:
    """Initialize the workspace: defaults sync, rule sync, config merge, cleanup."""
    logger.info("Workspace init started home=%s", paths.ductor_home)
    _migrate_tasks_to_cron_tasks(paths)
    sync_bundled_skills(paths)
    _sync_home_defaults(paths)
    _ensure_required_dirs(paths)

    # Deploy provider-specific rule files based on CLI auth status
    try:
        selector = RulesSelector(paths)
        selector.deploy_rules()
    except Exception:
        logger.exception("Failed to deploy rule files")

    sync_rule_files(paths.workspace)
    _smart_merge_config(paths)
    _clean_orphan_symlinks(paths)
    sync_skills(paths)
    logger.info("Workspace init completed")


# ---------------------------------------------------------------------------
# Runtime environment injection
# ---------------------------------------------------------------------------

_DOCKER_NOTICE = """

---

## Runtime Environment

**IMPORTANT: YOU ARE RUNNING INSIDE A DOCKER CONTAINER (`{container}`).**

- Your filesystem is isolated. `/ductor` is the mounted host directory `~/.ductor`.
- You cannot see or access the host system outside this mount.
- Feel free to experiment -- the host is protected.
"""

_HOST_NOTICE = """

---

## Runtime Environment

**WARNING: YOU ARE RUNNING DIRECTLY ON THE HOST SYSTEM. THERE IS NO SANDBOX.**

- Every file operation, command, and script runs on the user's real machine.
- Be careful with destructive commands (`rm -rf`, `chmod`, etc.).
- Ask before touching anything outside `workspace/`.
"""


def inject_runtime_environment(paths: DuctorPaths, *, docker_container: str) -> None:
    """Append a runtime environment section to workspace CLAUDE.md + AGENTS.md.

    Called once after workspace init when the Docker state is known.
    """
    notice = _DOCKER_NOTICE.format(container=docker_container) if docker_container else _HOST_NOTICE
    for name in ("CLAUDE.md", "AGENTS.md"):
        target = paths.workspace / name
        if not target.exists():
            continue
        content = target.read_text(encoding="utf-8")
        # Avoid duplicate injection on restart without workspace re-init
        if "## Runtime Environment" in content:
            continue
        target.write_text(content + notice, encoding="utf-8")
    logger.info(
        "Runtime environment injected: %s",
        "docker" if docker_container else "host",
    )


_RULE_SYNC_INTERVAL = 10.0  # seconds


async def watch_rule_files(workspace: Path, *, interval: float = _RULE_SYNC_INTERVAL) -> None:
    """Continuously sync CLAUDE.md <-> AGENTS.md across the workspace.

    Runs ``sync_rule_files`` in a thread every *interval* seconds so that
    changes made by either Claude (CLAUDE.md) or Codex (AGENTS.md) are
    propagated to the counterpart file automatically.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(sync_rule_files, workspace)
        except Exception:
            logger.exception("Rule file sync failed")
