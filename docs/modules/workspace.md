# workspace/

Workspace/home-directory management for `~/.ductor`.

## Files

- `paths.py`: immutable `DuctorPaths` + `resolve_paths()`
- `init.py`: workspace init pipeline, zone copy rules, rule sync, runtime notice injection
- `rules_selector.py`: auth-aware RULES template selection/deployment + stale file cleanup
- `cron_tasks.py`: create/list/delete cron-task folders
- `skill_sync.py`: cross-tool skill sync and bundled-skill sync
- `loader.py`: safe file readers

## `DuctorPaths`

Important runtime paths:

- `ductor_home`: `~/.ductor` (default)
- `workspace`: `~/.ductor/workspace`
- `config_path`: `~/.ductor/config/config.json`
- `sessions_path`: `~/.ductor/sessions.json`
- `named_sessions_path`: `~/.ductor/named_sessions.json`
- `env_file`: `~/.ductor/.env`
- `tasks_registry_path`: `~/.ductor/tasks.json`
- `chat_activity_path`: `~/.ductor/chat_activity.json`
- `startup_state_path`: `~/.ductor/startup_state.json`
- `inflight_turns_path`: `~/.ductor/inflight_turns.json`
- `cron_jobs_path`: `~/.ductor/cron_jobs.json`
- `webhooks_path`: `~/.ductor/webhooks.json`
- `logs_dir`: `~/.ductor/logs`
- `cron_tasks_dir`: `~/.ductor/workspace/cron_tasks`
- `tasks_dir`: `~/.ductor/workspace/tasks`
- `api_files_dir`: `~/.ductor/workspace/api_files`
- `skills_dir`: `~/.ductor/workspace/skills`
- `bundled_skills_dir`: package `_home_defaults/workspace/skills`

## `init_workspace()` order

1. migrate legacy `workspace/tasks -> workspace/cron_tasks`
2. `sync_bundled_skills(paths)`
3. `_sync_home_defaults(paths)`
4. ensure required directories
5. `RulesSelector(paths).deploy_rules()`
6. `ensure_task_rule_files(paths.cron_tasks_dir)`
7. `sync_rule_files(paths.workspace)`
8. `_smart_merge_config(paths)`
9. `_clean_orphan_symlinks(paths)`
10. `sync_skills(paths)`

Idempotent by design (called from multiple startup paths).

Directory creation note:

- `workspace/api_files/` is not in `_REQUIRED_DIRS`; it is created lazily on first API upload via `prepare_destination(...)`.
- `workspace/tasks/` is part of `_REQUIRED_DIRS` and always created (used by shared `TaskHub` task folders).
- sub-agent homes do not create `logs/` by default; all agents write to the main home log file `~/.ductor/logs/agent.log`.

## Zone copy rules (`_walk_and_copy`)

### Zone 2 (always overwritten)

- `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`
- `.py` files in:
  - `workspace/tools/cron_tools/`
  - `workspace/tools/webhook_tools/`
  - `workspace/tools/agent_tools/`
  - `workspace/tools/task_tools/`

Special case:

- when copying a template `CLAUDE.md`, init also writes mirrored `AGENTS.md` and `GEMINI.md` in same target dir.

### Zone 3 (seed once)

- all other files: copied only when missing.

Skipped template files (handled by `RulesSelector`):

- `RULES.md`
- `RULES-claude-only.md`
- `RULES-codex-only.md`
- `RULES-gemini-only.md`
- `RULES-all-clis.md`

## RULES deployment (`rules_selector.py`)

Template variants:

- `RULES.md`
- `RULES-claude-only.md`
- `RULES-codex-only.md`
- `RULES-gemini-only.md`
- `RULES-all-clis.md`

Variant selection:

- `all-clis` when 2+ providers are authenticated
- `codex-only` when only Codex (or only Grok — same `AGENTS.md` rules)
- `gemini-only` when only Gemini
- otherwise `claude-only`

Deployment outputs per authenticated provider:

- Claude -> `CLAUDE.md`
- Codex / Grok -> `AGENTS.md` (shared — like Codex, Grok loads project rules from `AGENTS.md`)
- Gemini -> `GEMINI.md`

Antigravity is a CLI provider, but workspace rule deployment has no dedicated
Antigravity rule-file target; it still deploys only the Claude, Codex, and Gemini
rule-file names above.

Cleanup removes stale provider files for unauthenticated providers, except inside `workspace/cron_tasks/` (user-owned task rules).

## Rule sync (`sync_rule_files`)

Recursive mtime-based sync for:

- `CLAUDE.md`
- `AGENTS.md`
- `GEMINI.md`

Per directory:

- pick newest existing file
- copy to outdated existing siblings
- missing siblings are generally not created, except task-folder backfill via `ensure_task_rule_files(...)`

Background watcher: `watch_rule_files(workspace, interval=10s)`.

Watcher detail per cycle:

1. `ensure_task_rule_files(cron_tasks_dir)` backfills missing provider rule files in existing task folders.
2. `sync_rule_files(workspace)` propagates newest content to older sibling rule files.

## Runtime environment injection

`inject_runtime_environment(paths, docker_container=...)` appends two sections to each existing workspace rule file (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`):

- `## Multi-Agent Identity` (main/sub-agent context + communication hints)
- Docker mode notice (`/ductor` mount)
- host mode warning (no sandbox)

Duplicate prevention: injection is skipped when either marker already exists (`## Multi-Agent Identity` or `## Runtime Environment`).

## Cron task folders (`cron_tasks.py`)

`create_cron_task(...)` creates:

```text
cron_tasks/<safe_name>/
  CLAUDE.md    # only if Claude authenticated
  AGENTS.md    # only if Codex and/or Grok authenticated
  GEMINI.md    # only if Gemini authenticated
  TASK_DESCRIPTION.md
  <safe_name>_MEMORY.md
  scripts/
```

Rule file selection is based on which provider rule files exist in the parent `cron_tasks/` directory (deployed by `RulesSelector` during workspace init). Falls back to `CLAUDE.md` when no parent rule files are found.

Path traversal protection is enforced for create/delete operations.

`sync_rule_files()` itself only updates already-existing rule files by mtime. Missing task-folder rule files are created by `ensure_task_rule_files(...)` in init and watcher cycles.

## Skill sync summary

`sync_skills()` syncs between:

- `~/.ductor/workspace/skills`
- `~/.claude/skills`
- `~/.codex/skills`
- `~/.gemini/skills`
- `~/.grok/skills` (or `$GROK_HOME/skills`)

Default mode uses symlinks/junctions. Docker mode uses managed directory copies (`.ductor_managed`) so paths resolve inside container namespace.

See `docs/modules/skill_system.md`.

## Loader helpers

- `read_file(path) -> str | None`
- `read_mainmemory(paths) -> str`
