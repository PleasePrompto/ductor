"""Cross-platform skill directory sync between ductor workspace and CLI tools.

Provides three-way symlink synchronization so skills installed via Claude Code,
Codex CLI, or the ductor workspace are visible to all agents.

Sync runs once during ``init_workspace`` and periodically as a background task.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

_SKIP_DIRS: frozenset[str] = frozenset(
    {".claude", ".system", ".git", ".venv", "__pycache__", "node_modules"}
)

_SKILL_SYNC_INTERVAL = 30.0


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _discover_skills(base: Path) -> dict[str, Path]:
    """Scan a skills directory and return ``{name: path}`` for valid entries.

    Skips hidden/internal directories and broken symlinks.
    Only includes subdirectories (plain files are ignored).
    """
    if not base.is_dir():
        return {}
    skills: dict[str, Path] = {}
    for entry in sorted(base.iterdir()):
        if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
            continue
        if entry.is_symlink():
            if entry.exists():
                skills[entry.name] = entry
            continue
        if entry.is_dir():
            skills[entry.name] = entry
    return skills


def _cli_skill_dirs() -> dict[str, Path]:
    """Return skill directories for installed CLIs.

    Only includes CLIs whose home directory exists on disk.
    Uses the same detection pattern as ``cli/auth.py``.
    """
    dirs: dict[str, Path] = {}
    claude_home = Path.home() / ".claude"
    if claude_home.is_dir():
        dirs["claude"] = claude_home / "skills"
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    if codex_home.is_dir():
        dirs["codex"] = codex_home / "skills"
    return dirs


# ---------------------------------------------------------------------------
# Canonical resolution
# ---------------------------------------------------------------------------


def _resolve_canonical(
    name: str,
    ductor: dict[str, Path],
    claude: dict[str, Path],
    codex: dict[str, Path],
) -> Path | None:
    """Find the canonical (real, non-symlink) path for a skill.

    Priority: ductor > claude > codex.
    Falls back to resolving the first valid symlink if no real dir exists.
    """
    for registry in (ductor, claude, codex):
        entry = registry.get(name)
        if entry is not None and not entry.is_symlink():
            return entry
    for registry in (ductor, claude, codex):
        entry = registry.get(name)
        if entry is not None and entry.is_symlink() and entry.exists():
            return entry.resolve()
    return None


# ---------------------------------------------------------------------------
# Cross-platform symlink creation
# ---------------------------------------------------------------------------


def _create_dir_link(link_path: Path, target: Path) -> None:
    """Create a directory symlink with Windows junction fallback.

    Linux/macOS/WSL: standard ``os.symlink``.
    Windows: tries ``os.symlink`` (requires Developer Mode or admin),
    then falls back to NTFS junction via ``mklink /J`` (no admin needed).
    """
    if not _IS_WINDOWS:
        link_path.symlink_to(target)
        return

    try:
        link_path.symlink_to(target, target_is_directory=True)
    except OSError:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            msg = f"Failed to create symlink or junction: {link_path} -> {target}"
            raise OSError(msg) from None


def _ensure_link(link_path: Path, target: Path) -> bool:
    """Idempotently ensure *link_path* is a symlink to *target*.

    Returns ``True`` if a new link was created, ``False`` if already correct
    or if *link_path* is a real directory (never destroyed).
    """
    if link_path.exists() and not link_path.is_symlink():
        return False
    if link_path.is_symlink():
        if link_path.resolve() == target.resolve():
            return False
        link_path.unlink()
    _create_dir_link(link_path, target)
    return True


# ---------------------------------------------------------------------------
# Broken link cleanup
# ---------------------------------------------------------------------------


def _clean_broken_links(directory: Path) -> int:
    """Remove broken symlinks in *directory*. Returns count removed."""
    if not directory.is_dir():
        return 0
    removed = 0
    for entry in directory.iterdir():
        if entry.is_symlink() and not entry.exists():
            entry.unlink()
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _link_skill_everywhere(
    skill_name: str,
    canonical: Path,
    all_dirs: dict[str, Path],
) -> None:
    """Create symlinks for *skill_name* in every location that lacks it."""
    for loc_name, base_dir in all_dirs.items():
        if not base_dir.is_dir():
            base_dir.mkdir(parents=True, exist_ok=True)
        link = base_dir / skill_name
        if link == canonical or (link.exists() and not link.is_symlink()):
            continue
        try:
            if _ensure_link(link, canonical):
                logger.info("Skill link created: %s -> %s", link, canonical)
        except OSError:
            logger.warning("Failed to link skill %s in %s", skill_name, loc_name, exc_info=True)


def sync_skills(paths: DuctorPaths) -> None:
    """Three-way skill directory sync: ductor workspace <-> CLI skill dirs.

    Safety guarantees:
    - Real directories are never overwritten or removed.
    - Existing valid symlinks pointing elsewhere are left alone.
    - Internal directories (.system, .claude, .git, .venv) are skipped.
    """
    cli_dirs = _cli_skill_dirs()
    all_dirs: dict[str, Path] = {"ductor": paths.skills_dir, **cli_dirs}

    registries = {name: _discover_skills(d) for name, d in all_dirs.items()}

    all_names: set[str] = set()
    for reg in registries.values():
        all_names.update(reg.keys())

    for skill_name in sorted(all_names):
        canonical = _resolve_canonical(
            skill_name,
            registries.get("ductor", {}),
            registries.get("claude", {}),
            registries.get("codex", {}),
        )
        if canonical is not None:
            _link_skill_everywhere(skill_name, canonical, all_dirs)

    for base_dir in all_dirs.values():
        removed = _clean_broken_links(base_dir)
        if removed:
            logger.info("Cleaned %d broken skill link(s) in %s", removed, base_dir)


async def watch_skill_sync(
    paths: DuctorPaths,
    *,
    interval: float = _SKILL_SYNC_INTERVAL,
) -> None:
    """Continuously sync skill directories across all agents.

    Runs ``sync_skills`` in a thread every *interval* seconds.
    Follows the same pattern as ``watch_rule_files``.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(sync_skills, paths)
        except Exception:
            logger.exception("Skill sync failed")
