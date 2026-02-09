# Skill System

Cross-platform skill directory sync between ductor workspace, Claude Code CLI, and Codex CLI. Skills added anywhere become visible everywhere via symlinks.

## Files

- `workspace/skill_sync.py`: sync algorithm, cross-platform link creation, async watcher.
- `workspace/paths.py`: `skills_dir` property on `DuctorPaths`.
- `workspace/init.py`: calls `sync_skills()` at startup.
- `orchestrator/core.py`: runs `watch_skill_sync()` as background task.

## Sync Directories

```
~/.ductor/workspace/skills/  <-->  ~/.claude/skills/  <-->  ~/.codex/skills/
```

Only directories where the parent CLI home exists are included. If `~/.codex/` does not exist, Codex is skipped. Detection mirrors `cli/auth.py` logic (`CODEX_HOME` env var respected).

## Algorithm

`sync_skills(paths)` runs synchronously (called via `asyncio.to_thread` in the watcher):

1. **Discover**: scan each skill directory for subdirectories (skip `.claude`, `.system`, `.git`, `.venv`, `__pycache__`, `node_modules`). Broken symlinks excluded, valid symlinks included.
2. **Collect**: union of all skill names across all directories.
3. **Resolve canonical**: for each skill, find the real (non-symlink) directory. Priority: **ductor > claude > codex**. If all entries are symlinks, resolve the first valid target.
4. **Link everywhere**: for each directory that lacks the skill, create a symlink to the canonical path. Real directories are never overwritten. Existing valid symlinks pointing elsewhere are left alone.
5. **Clean**: remove broken symlinks in all directories.

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

## Background Watcher

`watch_skill_sync(paths, *, interval=30.0)` runs as an asyncio task started by `Orchestrator.create()`:

```python
orch._skill_sync_task = asyncio.create_task(watch_skill_sync(paths))
```

Every 30 seconds it calls `sync_skills()` via `asyncio.to_thread()`. New skills appear in all locations within one interval. Broken symlinks from deleted skills are cleaned automatically.

Cancelled cleanly in `Orchestrator.shutdown()` alongside the rule sync task.

## Integration Points

### Startup

1. `init_workspace()` creates `workspace/skills/` directory (listed in `_REQUIRED_DIRS`).
2. `init_workspace()` calls `sync_skills(paths)` for initial sync.
3. `Orchestrator.create()` starts the background watcher.

### Home Defaults

`ductor_bot/_home_defaults/workspace/skills/CLAUDE.md` is a Zone 2 rule file (always overwritten on update) that documents the sync system for the workspace agent. It is seeded into `~/.ductor/workspace/skills/` at startup.

### Workspace Exports

`sync_skills` and `watch_skill_sync` are re-exported from `ductor_bot/workspace/__init__.py`.

## Public API

```python
from ductor_bot.workspace import sync_skills, watch_skill_sync

# One-shot sync (startup)
sync_skills(paths)

# Background loop (orchestrator)
task = asyncio.create_task(watch_skill_sync(paths, interval=30.0))
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
- **Priority resolution**: ductor workspace is canonical by default so the bot always owns the "truth" for skills created there.
- **Periodic polling over filesystem watchers**: consistent cross-platform behavior without `inotify`/`FSEvents`/`ReadDirectoryChanges` dependencies. 30s interval is a good balance between responsiveness and overhead.
- **CLI dir detection at runtime**: no config needed. If a CLI is installed later, its skills appear on next sync cycle.
