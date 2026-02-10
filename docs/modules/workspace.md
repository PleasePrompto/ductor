# workspace/

Workspace and home-directory management. Resolves runtime paths, seeds `~/.ductor` from packaged home defaults, syncs rule files, injects runtime environment notices, and manages cron task folders.

## Files

- `paths.py`: immutable `DuctorPaths` + `resolve_paths()`.
- `init.py`: `init_workspace()`, `_walk_and_copy()` zone rules, required-dir creation, rule-file sync, runtime environment injection, config merge.
- `loader.py`: safe file readers (`read_file`, `read_mainmemory`).
- `cron_tasks.py`: create/list/delete cron task mini-workspaces + template render helpers.
- `skill_sync.py`: cross-platform skill directory sync (see [skill_system](skill_system.md)).

## `DuctorPaths`

Important properties (runtime side):

- `ductor_home`: `~/.ductor` (default, overridable)
- `workspace`: `~/.ductor/workspace`
- `config_path`: `~/.ductor/config/config.json`
- `sessions_path`: `~/.ductor/sessions.json`
- `cron_jobs_path`: `~/.ductor/cron_jobs.json`
- `webhooks_path`: `~/.ductor/webhooks.json`
- `logs_dir`: `~/.ductor/logs`
- `cron_tasks_dir`: `~/.ductor/workspace/cron_tasks`
- `tools_dir`: `~/.ductor/workspace/tools`
- `user_tools_dir`: `~/.ductor/workspace/tools/user_tools`
- `telegram_files_dir`: `~/.ductor/workspace/telegram_files`
- `output_to_user_dir`: `~/.ductor/workspace/output_to_user`
- `skills_dir`: `~/.ductor/workspace/skills`
- `bundled_skills_dir`: `ductor_bot/_home_defaults/workspace/skills` (package-internal, read-only)
- `mainmemory_path`: `~/.ductor/workspace/memory_system/MAINMEMORY.md`

Template source side:

- `home_defaults`: `ductor_bot/_home_defaults/` (mirrors `~/.ductor` layout 1:1)
- `config_example_path`: `<repo>/config.example.json` in source checkouts, otherwise bundled fallback `ductor_bot/_config_example.json`

## `init_workspace()` Flow

1. one-time migration: `workspace/tasks` -> `workspace/cron_tasks`.
2. link bundled skills from package via `sync_bundled_skills(paths)`.
3. sync home defaults (`paths.home_defaults`) into runtime home via `_walk_and_copy()` (skips already-symlinked targets).
4. ensure required directories exist (`workspace/*`, `config/`, `logs/`).
5. sync `CLAUDE.md` <-> `AGENTS.md` under `paths.workspace`.
6. shallow config merge with `config.example.json` (`_smart_merge_config`).
7. remove orphan symlinks in workspace root.
8. run `sync_skills(paths)` for cross-platform skill directory sync.

This function is intentionally called from both `__main__.py` and `Orchestrator.create()`. Behavior is idempotent and rule-based, so repeated execution is safe.

## Copy Rules (`_walk_and_copy`)

- Zone 2 (always overwrite): `CLAUDE.md`, `AGENTS.md`.
- Zone 3 (seed once): all other files copied only if missing.
- Special rule: copied `CLAUDE.md` also auto-copies to sibling `AGENTS.md`.
- Skips targets that are already symlinks (preserves bundled skill links).
- Skips hidden/ignored dirs (`.venv`, `.git`, `.mypy_cache`, `__pycache__`, `node_modules`).

## Rule Sync

`sync_rule_files(root)` runs recursively:

- only `CLAUDE.md` exists -> create `AGENTS.md`
- only `AGENTS.md` exists -> create `CLAUDE.md`
- both exist -> newer file (mtime) overwrites older file

`watch_rule_files(workspace, interval=10s)` runs this continuously in background.

## Runtime Environment Injection

`inject_runtime_environment(paths, docker_container=...)` appends a runtime notice to `workspace/CLAUDE.md` and `workspace/AGENTS.md`:

- Docker mode: informs the agent it runs inside container with `/ductor` mount.
- Host mode: warns the agent it runs directly on host system.

Injection is idempotent (`"## Runtime Environment"` marker check).

## Cron Task Workspaces (`cron_tasks.py`)

`create_cron_task(paths, name, title, description, with_venv=False)` creates:

```text
cron_tasks/<safe_name>/
  CLAUDE.md
  AGENTS.md
  TASK_DESCRIPTION.md
  <safe_name>_MEMORY.md
  scripts/
  [.venv/]  # optional
```

Key behaviors:

- task name is sanitized + traversal-protected,
- `CLAUDE.md` and `AGENTS.md` are generated from the same rendered template,
- `delete_cron_task()` validates resolved path stays inside `cron_tasks_dir`.

## Skill Sync

Three-way symlink sync between `~/.ductor/workspace/skills/`, `~/.claude/skills/`, and `~/.codex/skills/`. Skills added anywhere appear everywhere. Background watcher runs every 30 seconds. Full documentation in [skill_system](skill_system.md).

## Loader API

- `read_file(path) -> str | None`: tolerant reader, logs `OSError` and returns `None`.
- `read_mainmemory(paths) -> str`: returns `MAINMEMORY.md` content or empty string.
