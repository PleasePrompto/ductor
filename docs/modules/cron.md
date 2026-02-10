# cron/

In-process cron scheduling with JSON persistence.

## Files

- `manager.py`: `CronJob`, `CronManager` (CRUD + persistence).
- `observer.py`: `CronObserver` (schedule, watch, execute, callback).
- `execution.py`: provider command builders + output parsers + prompt enrichment.

## Data Model (`CronJob`)

Core fields:

- `id`, `title`, `description`, `schedule`
- `task_folder`
- `agent_instruction`
- `enabled`
- `timezone` (optional IANA string, overrides global `user_timezone` for this job)
- `created_at`
- `last_run_at`, `last_run_status`

Per-job execution overrides:

- `provider` (`"claude"`/`"codex"` or `null`)
- `model` (`str` or `null`)
- `reasoning_effort` (`str` or `null`, Codex only)
- `cli_parameters` (`list[str]`, default `[]`)

Missing/`null` override fields fall back to global config via `resolve_cli_config()`.

## Persistence (`CronManager`)

- File: `~/.ductor/cron_jobs.json`
- Format: `{ "jobs": [ ... ] }`
- Save mode: atomic temp-write + replace.

API:

- `add_job(job)`
- `remove_job(job_id)`
- `list_jobs()`
- `get_job(job_id)`
- `update_run_status(job_id, status=...)`
- `reload()`

## Observer Lifecycle (`CronObserver`)

`start()`:

1. schedule all enabled jobs,
2. start file watcher loop.

Watcher behavior:

- poll jobs file mtime every 5s,
- on change: `manager.reload()` + cancel/reschedule all jobs.

`stop()`:

- cancel watcher,
- cancel all scheduled tasks.

## Job Execution Path

When a job is due:

1. validate task folder exists (`workspace/cron_tasks/<task_folder>`).
2. build `TaskOverrides` from the job (`provider`, `model`, `reasoning_effort`, `cli_parameters`).
3. resolve `TaskExecutionConfig` via `resolve_cli_config(config, codex_cache, task_overrides=...)`.
4. enrich instruction with `<task_folder>_MEMORY.md` reminder.
5. build provider command via `build_cmd(exec_config, prompt)`.
6. run subprocess in task folder (`stdin=DEVNULL`).
7. enforce timeout (`config.cli_timeout`).
8. parse result (`parse_claude_result` or `parse_codex_result`).
9. persist run status.
10. fire optional callback `(title, result_text, status)`.
11. reschedule next run.

## Status Codes

Typical status values:

- `success`
- `error:folder_missing`
- `error:cli_not_found_claude`
- `error:cli_not_found_codex`
- `error:timeout`
- `error:exit_<code>`

## `execution.py` Command Rules

`build_cmd(exec_config, prompt)`:

- Claude:
  - `claude -p --output-format json --model <model> --permission-mode <mode> --no-session-persistence [<cli_parameters...>] -- <prompt>`
- Codex:
  - `codex exec --json --color never --skip-git-repo-check ...`
  - `bypassPermissions` -> `--dangerously-bypass-approvals-and-sandbox`
  - otherwise -> `--full-auto`
  - reasoning effort flag is appended only when value is non-empty and not `"medium"`
  - task `cli_parameters` are inserted before `--`

## Override Resolution Notes

`resolve_cli_config()` enforces:

- Claude model must be one of `haiku`/`sonnet`/`opus`.
- Codex model must exist in `CodexModelCache`.
- Codex reasoning effort is used only if the selected model supports it.

Current behavior: cron `cli_parameters` are taken from the job entry itself (no merge with global `AgentConfig.cli_parameters`).

## Timezone Handling

Cron expressions are interpreted in the user's timezone, not UTC.

Resolution order per job:

1. `CronJob.timezone` (per-job override).
2. `AgentConfig.user_timezone` (global config).
3. Host system timezone (via `$TZ` or `/etc/localtime`).
4. `UTC` (fallback).

Implementation: `_schedule_job()` converts `datetime.now()` to the resolved timezone, passes naive local time to `CronSim`, then re-attaches the timezone to compute UTC delay.

Workspace tools: `cron_add.py` and `cron_edit.py` accept `--timezone`. `cron_time.py` shows current time in configured/common timezones. `cron_list.py` reports the active `user_timezone`.

## Design Choice

- No system crontab integration.
- Scheduling is fully in-process and tied to bot lifecycle.
- `cron_jobs.json` is the single source of truth.
