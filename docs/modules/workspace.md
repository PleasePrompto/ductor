# workspace/

Workspace and home-directory management. Resolves runtime paths, seeds `~/.ductor` from packaged home defaults, syncs rule files, and manages cron task folders.

## Files

- `paths.py`: immutable `DuctorPaths` + `resolve_paths()`.
- `init.py`: `init_workspace()`, `_walk_and_copy()` zone rules, rule-file sync, config merge.
- `loader.py`: safe file readers (`read_file`, `read_mainmemory`).
- `cron_tasks.py`: create/list/delete cron task mini-workspaces + template render helpers.

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
- `mainmemory_path`: `~/.ductor/workspace/memory_system/MAINMEMORY.md`

Template source side:

- `home_defaults`: `ductor_bot/_home_defaults/` (mirrors `~/.ductor` layout 1:1)
- `config_example_path`: `<repo>/config.example.json` in source checkouts, otherwise bundled fallback `ductor_bot/_config_example.json`

## `init_workspace()` Flow

1. one-time migration: `workspace/tasks` -> `workspace/cron_tasks`.
2. sync home defaults (`paths.home_defaults`) into runtime home via `_walk_and_copy()`.
3. sync `CLAUDE.md` <-> `AGENTS.md` under `paths.workspace`.
4. shallow config merge with `config.example.json` (`_smart_merge_config`).
5. remove orphan symlinks in workspace root.

This function is intentionally called from both `__main__.py` and `Orchestrator.create()`. Behavior is idempotent and rule-based, so repeated execution is safe.

## Copy Rules (`_walk_and_copy`)

- Zone 2 (always overwrite): `CLAUDE.md`, `AGENTS.md`.
- Zone 3 (seed once): all other files copied only if missing.
- Special rule: copied `CLAUDE.md` also auto-copies to sibling `AGENTS.md`.
- skips hidden/ignored dirs (`.venv`, `.git`, `.mypy_cache`, `__pycache__`, `node_modules`).

## Rule Sync

`sync_rule_files(root)` runs recursively:

- only `CLAUDE.md` exists -> create `AGENTS.md`
- only `AGENTS.md` exists -> create `CLAUDE.md`
- both exist -> newer file (mtime) overwrites older file

`watch_rule_files(workspace, interval=10s)` runs this continuously in background.

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

## Loader API

- `read_file(path) -> str | None`: tolerant reader, logs `OSError` and returns `None`.
- `read_mainmemory(paths) -> str`: returns `MAINMEMORY.md` content or empty string.
