"""Cross-platform skill directory sync between ductor workspace and CLI tools.

Provides multi-way symlink synchronization so skills installed via Claude Code,
Codex CLI, Gemini CLI, or the ductor workspace are visible to all agents.

Includes bundled-skill linking (package → workspace), sync-time external-symlink
protection, and cleanup of ductor-created links on shutdown.

When Docker sandboxing is active, symlinks are replaced with directory copies
(marked with ``.ductor_managed``) because absolute host paths do not resolve
inside the container's mount namespace.

Sync runs once during ``init_workspace`` and periodically as a background task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

_SKIP_DIRS: frozenset[str] = frozenset(
    {".claude", ".system", ".git", ".venv", "__pycache__", "node_modules"}
)

_SKILL_SYNC_INTERVAL = 30.0
_MANAGED_MARKER = ".ductor_managed"
_SYNCABLE_PROVIDERS: frozenset[str] = frozenset({"claude", "codex", "gemini", "grok"})


def _load_skill_sync_config(config_path: Path) -> tuple[bool, frozenset[str]]:
    """Read skill-sync toggles from config.json (#141).

    Returns ``(sync_enabled, enabled_providers)``. Defaults to fully enabled
    when the file is absent, unreadable, or omits the ``skills`` block, so
    existing installs keep their current behavior. Read live on every sync so
    config hot-reload takes effect on the next tick.
    """
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return True, _SYNCABLE_PROVIDERS
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, dict):
        return True, _SYNCABLE_PROVIDERS
    sync = skills.get("sync")
    if isinstance(sync, dict):
        enabled_providers = frozenset(p for p in _SYNCABLE_PROVIDERS if sync.get(p, True))
    else:
        enabled_providers = _SYNCABLE_PROVIDERS
    return bool(skills.get("sync_enabled", True)), enabled_providers


def _is_under(child: Path, parent: Path) -> bool:
    """Return ``True`` if *child* is located under *parent* directory."""
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    else:
        return True


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
            if entry.exists() and _has_valid_skill_frontmatter(entry):
                skills[entry.name] = entry
            continue
        if entry.is_dir() and _has_valid_skill_frontmatter(entry):
            skills[entry.name] = entry
    return skills


def _has_valid_skill_frontmatter(skill_dir: Path) -> bool:
    """Return True when ``SKILL.md`` has Codex/Claude-compatible frontmatter."""
    skill_md = skill_dir / "SKILL.md"
    try:
        lines = skill_md.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if not lines or lines[0].strip() != "---":
        return False
    end_index = -1
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index <= 1:
        return False
    try:
        metadata = yaml.safe_load("\n".join(lines[1:end_index]))
    except yaml.YAMLError:
        return False
    if not isinstance(metadata, dict):
        return False
    name = metadata.get("name")
    description = metadata.get("description")
    return (
        isinstance(name, str)
        and bool(name.strip())
        and isinstance(description, str)
        and bool(description.strip())
    )


def _cli_skill_dirs(enabled_providers: frozenset[str] | None = None) -> dict[str, Path]:
    """Return skill directories for installed CLIs.

    Only includes CLIs whose home directory exists on disk and whose sync is
    enabled. ``enabled_providers=None`` (the default) means all providers.
    Uses the same detection pattern as ``cli/auth.py``.
    """
    dirs: dict[str, Path] = {}
    if enabled_providers is None or "claude" in enabled_providers:
        claude_home = Path.home() / ".claude"
        if claude_home.is_dir():
            dirs["claude"] = claude_home / "skills"
    if enabled_providers is None or "codex" in enabled_providers:
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        if codex_home.is_dir():
            dirs["codex"] = codex_home / "skills"
    if enabled_providers is None or "gemini" in enabled_providers:
        gemini_home = Path.home() / ".gemini"
        if gemini_home.is_dir():
            dirs["gemini"] = gemini_home / "skills"
    if enabled_providers is None or "grok" in enabled_providers:
        grok_home = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok")))
        if grok_home.is_dir():
            dirs["grok"] = grok_home / "skills"
    return dirs


# ---------------------------------------------------------------------------
# Canonical resolution
# ---------------------------------------------------------------------------


def _resolve_canonical(
    name: str,
    *registries: dict[str, Path],
) -> Path | None:
    """Find the canonical (real, non-symlink) path for a skill.

    Priority follows argument order (typically ductor > claude > codex > gemini).
    Falls back to resolving the first valid symlink if no real dir exists.
    """
    for registry in registries:
        entry = registry.get(name)
        if entry is not None and not entry.is_symlink():
            return entry
    for registry in registries:
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
# Docker-aware copy helpers
# ---------------------------------------------------------------------------


def _is_managed_copy(path: Path) -> bool:
    """Return ``True`` if *path* is a ductor-managed copy (has marker file)."""
    return path.is_dir() and not path.is_symlink() and (path / _MANAGED_MARKER).is_file()


def _newest_mtime(directory: Path) -> float:
    """Return the newest mtime of any file or directory under *directory*.

    Tolerates files disappearing during iteration (concurrent modifications).
    """
    newest = directory.stat().st_mtime
    for entry in directory.rglob("*"):
        try:
            newest = max(newest, entry.stat().st_mtime)
        except OSError:
            continue
    return newest


def _ensure_copy(dest: Path, source: Path) -> bool:
    """Copy *source* directory to *dest* with a ``.ductor_managed`` marker.

    Skips the copy when *dest* already has the marker and *source* has not
    been modified since the last copy (recursive mtime comparison).

    Tolerates concurrent modifications from other agents running skill sync
    in parallel (marker may vanish between check and stat, rmtree may race
    with Python's import machinery or another agent's copytree).

    Returns ``True`` if a new copy was made.
    """
    marker = dest / _MANAGED_MARKER
    if _is_managed_copy(dest):
        try:
            if _newest_mtime(source) <= marker.stat().st_mtime:
                return False
        except OSError:
            pass  # marker removed concurrently — proceed with fresh copy
        shutil.rmtree(dest, ignore_errors=True)
    elif dest.exists() and not dest.is_symlink():
        return False

    if dest.is_symlink():
        dest.unlink()

    shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
    marker.touch()
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


def _should_skip_link(dest: Path, sync_roots: frozenset[Path]) -> bool:
    """Return ``True`` if *dest* should be left alone during symlink sync."""
    if dest.exists() and not dest.is_symlink():
        return True
    if dest.is_symlink() and dest.exists():
        resolved = dest.resolve()
        return not any(_is_under(resolved, root) for root in sync_roots)
    return False


def _should_skip_copy(dest: Path) -> bool:
    """Return ``True`` if *dest* should be left alone during copy sync."""
    return dest.exists() and not dest.is_symlink() and not _is_managed_copy(dest)


def _link_skill_everywhere(
    skill_name: str,
    canonical: Path,
    all_dirs: dict[str, Path],
    *,
    use_copies: bool = False,
) -> None:
    """Create symlinks (or copies) for *skill_name* in every location that lacks it.

    Preserves existing symlinks that point outside the known sync directories
    (user-managed external links are never touched).

    When *use_copies* is ``True`` (Docker mode), directories are copied
    instead of symlinked so they resolve inside the container.
    """
    sync_roots = frozenset(d.resolve() for d in all_dirs.values() if d.is_dir())
    for loc_name, base_dir in all_dirs.items():
        if not base_dir.is_dir():
            base_dir.mkdir(parents=True, exist_ok=True)
        dest = base_dir / skill_name
        if dest == canonical:
            continue
        skip = _should_skip_copy(dest) if use_copies else _should_skip_link(dest, sync_roots)
        if skip:
            continue
        try:
            if use_copies:
                if _ensure_copy(dest, canonical):
                    logger.info("Skill copied: %s -> %s", dest, canonical)
            elif _ensure_link(dest, canonical):
                logger.info("Skill link created: %s -> %s", dest, canonical)
        except OSError:
            logger.warning("Failed to sync skill %s in %s", skill_name, loc_name, exc_info=True)


def sync_skills(paths: DuctorPaths, *, docker_active: bool = False) -> None:
    """Multi-way skill directory sync: ductor workspace <-> CLI skill dirs.

    Syncs between ductor workspace, ~/.claude/skills, ~/.codex/skills,
    and ~/.gemini/skills.

    When *docker_active* is ``True``, copies are used instead of symlinks
    so skills resolve inside the Docker container.

    Safety guarantees:
    - Real directories are never overwritten or removed.
    - Existing valid symlinks pointing elsewhere are left alone.
    - Internal directories (.system, .claude, .git, .venv) are skipped.

    Honors the ``skills`` config block (#141): a global ``sync_enabled=false``
    skips entirely, and per-provider toggles drop individual CLI directories.
    """
    sync_enabled, enabled_providers = _load_skill_sync_config(paths.config_path)
    if not sync_enabled:
        logger.debug("Cross-tool skill sync disabled via config; skipping")
        return
    cli_dirs = _cli_skill_dirs(enabled_providers)
    all_dirs: dict[str, Path] = {"ductor": paths.skills_dir, **cli_dirs}

    removed_invalid = _clean_invalid_workspace_skill_links(paths.skills_dir)
    if removed_invalid:
        logger.info(
            "Removed %d invalid workspace skill link(s) without SKILL.md frontmatter",
            removed_invalid,
        )

    registries = {name: _discover_skills(d) for name, d in all_dirs.items()}

    all_names: set[str] = set()
    for reg in registries.values():
        all_names.update(reg.keys())

    # Priority order: ductor > claude > codex > gemini
    priority = ("ductor", "claude", "codex", "gemini", "grok")
    for skill_name in sorted(all_names):
        canonical = _resolve_canonical(
            skill_name,
            *(registries.get(n, {}) for n in priority),
        )
        if canonical is not None:
            _link_skill_everywhere(skill_name, canonical, all_dirs, use_copies=docker_active)

    for base_dir in all_dirs.values():
        removed = _clean_broken_links(base_dir)
        if removed:
            logger.info("Cleaned %d broken skill link(s) in %s", removed, base_dir)


def _clean_invalid_workspace_skill_links(base_dir: Path) -> int:
    """Remove Ductor workspace links/copies that point at invalid skill folders.

    User-owned real directories are preserved. Removing invalid workspace
    symlinks prevents Codex from loading legacy synced skills whose ``SKILL.md``
    is missing required YAML frontmatter.
    """
    if not base_dir.is_dir():
        return 0
    removed = 0
    for entry in sorted(base_dir.iterdir()):
        if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
            continue
        if entry.is_symlink() and entry.exists() and not _has_valid_skill_frontmatter(entry):
            entry.unlink()
            removed += 1
            continue
        if _is_managed_copy(entry) and not _has_valid_skill_frontmatter(entry):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def _iter_bundled_entries(paths: DuctorPaths) -> list[tuple[Path, Path]]:
    """Return ``(source, target)`` pairs for each bundled skill."""
    bundled = paths.bundled_skills_dir
    if not bundled.is_dir():
        return []
    target_dir = paths.skills_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    pairs: list[tuple[Path, Path]] = []
    for entry in sorted(bundled.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in _SKIP_DIRS:
            continue
        if not _has_valid_skill_frontmatter(entry):
            logger.warning("Skipping bundled skill without valid frontmatter: %s", entry)
            continue
        pairs.append((entry, target_dir / entry.name))
    return pairs


def sync_bundled_skills(paths: DuctorPaths, *, docker_active: bool = False) -> None:
    """Sync bundled skills from the package into the ductor workspace.

    Creates symlinks (or copies when *docker_active*) from
    ``~/.ductor/workspace/skills/<name>`` to the package's
    ``_home_defaults/workspace/skills/<name>`` so bundled skills
    stay up-to-date with the installed ductor version.

    Real directories are never overwritten (preserves user modifications
    from older Zone 3 copies or manually created skills with the same name).
    """
    for source, target in _iter_bundled_entries(paths):
        if docker_active:
            try:
                if _ensure_copy(target, source):
                    logger.info("Bundled skill copied: %s -> %s", target, source)
            except OSError:
                logger.warning("Failed to copy bundled skill %s", source.name, exc_info=True)
            continue

        if target.exists() and not target.is_symlink():
            continue
        if target.is_symlink():
            if target.resolve() == source.resolve():
                continue
            target.unlink()
        try:
            _create_dir_link(target, source)
            logger.info("Bundled skill linked: %s -> %s", target, source)
        except OSError:
            logger.warning("Failed to link bundled skill %s", source.name, exc_info=True)


def cleanup_ductor_links(paths: DuctorPaths) -> int:
    """Remove symlinks created by ductor in CLI skill directories.

    Only removes symlinks whose resolved target is under the ductor workspace
    skills directory or the bundled skills directory.  Everything else
    (real directories, user-managed symlinks) is left untouched.

    Returns the total count of removed links.
    """
    managed_roots = [paths.skills_dir]
    bundled = paths.bundled_skills_dir
    if bundled.is_dir():
        managed_roots.append(bundled)

    removed = 0
    for cli_dir in _cli_skill_dirs().values():
        if not cli_dir.is_dir():
            continue
        for entry in cli_dir.iterdir():
            if not entry.is_symlink():
                continue
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if any(_is_under(resolved, root) for root in managed_roots):
                entry.unlink()
                removed += 1
                logger.info("Removed ductor skill link: %s", entry)

    if removed:
        logger.info("Cleaned up %d ductor skill link(s) from CLI directories", removed)
    return removed


async def watch_skill_sync(
    paths: DuctorPaths,
    *,
    docker_active: bool = False,
    interval: float = _SKILL_SYNC_INTERVAL,
) -> None:
    """Continuously sync skill directories across all agents.

    Runs ``sync_skills`` in a thread every *interval* seconds.
    Follows the same pattern as ``watch_rule_files``.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(sync_skills, paths, docker_active=docker_active)
        except Exception:
            logger.exception("Skill sync failed")
