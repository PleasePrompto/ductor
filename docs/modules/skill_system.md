# Skill System

Cross-platform skill directory sync between ductor workspace, Claude Code CLI, and Codex CLI. Skills added anywhere become visible everywhere via symlinks. Includes bundled skill distribution, external symlink protection, and clean shutdown.

## Files

- `workspace/skill_sync.py`: sync algorithm, bundled skills, cleanup, cross-platform link creation, async watcher.
- `workspace/paths.py`: `skills_dir` and `bundled_skills_dir` properties on `DuctorPaths`.
- `workspace/init.py`: calls `sync_bundled_skills()` and `sync_skills()` at startup.
- `orchestrator/core.py`: runs `watch_skill_sync()` as background task, calls `cleanup_ductor_links()` on shutdown.

## Sync Directories

```
~/.ductor/workspace/skills/  <-->  ~/.claude/skills/  <-->  ~/.codex/skills/
```

Only directories where the parent CLI home exists are included. If `~/.codex/` does not exist, Codex is skipped. Detection mirrors `cli/auth.py` logic (`CODEX_HOME` env var respected).

## Bundled Skills

Skills shipped with the ductor package live in `ductor_bot/_home_defaults/workspace/skills/`. They are linked into the workspace via `sync_bundled_skills(paths)`:

```
~/.ductor/workspace/skills/<name>  -->  <package>/_home_defaults/workspace/skills/<name>
```

- Runs **before** `_sync_home_defaults()` so `_walk_and_copy()` skips already-symlinked targets.
- Real directories with the same name are **never overwritten** (user modifications are preserved).
- Stale symlinks (pointing to an old package path) are updated automatically.
- Always up-to-date with the installed ductor version (symlink, not copy).

## Sync Algorithm

`sync_skills(paths)` runs synchronously (called via `asyncio.to_thread` in the watcher):

1. **Discover**: scan each skill directory for subdirectories (skip `.claude`, `.system`, `.git`, `.venv`, `__pycache__`, `node_modules`). Broken symlinks excluded, valid symlinks included.
2. **Collect**: union of all skill names across all directories.
3. **Resolve canonical**: for each skill, find the real (non-symlink) directory. Priority: **ductor > claude > codex**. If all entries are symlinks, resolve the first valid target.
4. **Link everywhere**: for each directory that lacks the skill, create a symlink to the canonical path. Real directories are never overwritten. External symlinks (pointing outside the sync directories) are preserved.
5. **Clean**: remove broken symlinks in all directories.

## External Symlink Protection

During sync, existing symlinks in any directory are checked before replacement:

- If a symlink's resolved target is **under** one of the three sync directories, it is considered ductor-managed and may be updated to point to a new canonical source.
- If a symlink points **outside** all sync directories (e.g. `~/my-agents/skills/foo`), it is treated as user-managed and **never touched**.

This ensures that users who symlink skills from external locations keep their setup intact.

## Shutdown Cleanup

`cleanup_ductor_links(paths)` runs during `Orchestrator.shutdown()`:

- Scans `~/.claude/skills/` and `~/.codex/skills/`.
- Removes **only** symlinks whose resolved target is under `paths.skills_dir` or `paths.bundled_skills_dir`.
- Real directories and user-managed symlinks are left untouched.
- Prevents orphan symlinks after ductor stops or is uninstalled.

## Cross-Platform Link Creation

`_create_dir_link(link_path, target)`:

| Platform | Method |
|----------|--------|
| Linux / macOS / WSL | `Path.symlink_to(target)` |
| Windows | `os.symlink(target, link_path, target_is_directory=True)` |
| Windows fallback | `cmd /c mklink /J <link> <target>` (NTFS junction, no admin required) |

If all methods fail (e.g. restricted Windows without developer mode), the error is logged and the skill is skipped.

## Safety Guarantees

- Real directories are **never** overwritten or removed.
- Existing valid symlinks pointing to external locations (e.g. `~/.agents/...`) are left untouched.
- Internal directories (`.system`, `.claude`, `.git`, etc.) are always skipped.
- `CLAUDE.md` / `AGENTS.md` files in the ductor `skills/` directory are not synced (discovery only picks up subdirectories).
- The sync function runs in a worker thread -- no concurrent mutation of the skill directories.
- On shutdown, only ductor-created symlinks are removed from CLI directories.

## Background Watcher

`watch_skill_sync(paths, *, interval=30.0)` runs as an asyncio task started by `Orchestrator.create()`:

```python
orch._skill_sync_task = asyncio.create_task(watch_skill_sync(paths))
```

Every 30 seconds it calls `sync_skills()` via `asyncio.to_thread()`. New skills appear in all locations within one interval. Broken symlinks from deleted skills are cleaned automatically.

Cancelled cleanly in `Orchestrator.shutdown()` alongside the rule sync task.

## Integration Points

### Startup

1. `init_workspace()` calls `sync_bundled_skills(paths)` to link package-bundled skills.
2. `init_workspace()` calls `_sync_home_defaults()` (skips already-symlinked skill dirs).
3. `init_workspace()` creates `workspace/skills/` directory (listed in `_REQUIRED_DIRS`).
4. `init_workspace()` calls `sync_skills(paths)` for initial three-way sync.
5. `Orchestrator.create()` starts the background watcher.

### Shutdown

1. `Orchestrator.shutdown()` cancels the background watcher.
2. `Orchestrator.shutdown()` calls `cleanup_ductor_links(paths)` to remove ductor-created symlinks from CLI directories.

### Home Defaults

`ductor_bot/_home_defaults/workspace/skills/CLAUDE.md` is a Zone 2 rule file (always overwritten on update) that documents the sync system for the workspace agent.

Skill subdirectories in `_home_defaults/workspace/skills/` (e.g. `skill-creator/`) are linked via `sync_bundled_skills()` -- not copied via Zone 3.

### Workspace Exports

`sync_skills`, `sync_bundled_skills`, `cleanup_ductor_links`, and `watch_skill_sync` are re-exported from `ductor_bot/workspace/__init__.py`.

## Public API

```python
from ductor_bot.workspace import (
    cleanup_ductor_links,
    sync_bundled_skills,
    sync_skills,
    watch_skill_sync,
)

# Link bundled skills from package (startup, before home defaults)
sync_bundled_skills(paths)

# One-shot three-way sync (startup)
sync_skills(paths)

# Background loop (orchestrator)
task = asyncio.create_task(watch_skill_sync(paths, interval=30.0))

# Cleanup on shutdown (removes ductor symlinks from CLI dirs)
cleanup_ductor_links(paths)
```

## Skill Format

A skill is a subdirectory containing at minimum a `SKILL.md` file:

```
skills/
  my-skill/
    SKILL.md        # required: skill instructions
    scripts/        # optional: helper scripts
    ...
```

Skills with nested subdirectories and scripts are linked correctly -- the symlink points to the top-level skill directory.

## Design Choices

- **Symlinks over copies**: zero duplication, changes propagate instantly. Junction fallback covers Windows without developer mode.
- **Bundled skills as symlinks**: always reflects the installed package version. No Zone 3 staleness.
- **Priority resolution**: ductor workspace is canonical by default so the bot always owns the "truth" for skills created there.
- **External symlink protection**: user-managed symlinks pointing outside sync dirs are never replaced.
- **Shutdown cleanup**: ductor-created symlinks are removed from CLI dirs, preventing orphans after uninstall.
- **Periodic polling over filesystem watchers**: consistent cross-platform behavior without `inotify`/`FSEvents`/`ReadDirectoryChanges` dependencies. 30s interval is a good balance between responsiveness and overhead.
- **CLI dir detection at runtime**: no config needed. If a CLI is installed later, its skills appear on next sync cycle.
