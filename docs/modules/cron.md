# cron/

In-process cron scheduling with JSON persistence.

## Files

- `manager.py`: `CronJob`, `CronManager` (CRUD + persistence).
- `observer.py`: `CronObserver` (schedule, watch, execute, callback).
- `execution.py`: provider command builders + output parsers + prompt enrichment.

## Data Model (`CronJob`)

- `id`, `title`, `description`, `schedule`
- `task_folder`
- `agent_instruction`
- `enabled`
- `timezone` (optional IANA string, overrides global `user_timezone` for this job)
- `created_at`
- `last_run_at`, `last_run_status`

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
2. snapshot model/provider/permission mode from config.
3. enrich instruction with `<task_folder>_MEMORY.md` reminder.
4. build provider command (`build_cmd`).
5. run subprocess in task folder (`stdin=DEVNULL`).
6. enforce timeout (`config.cli_timeout`).
7. parse result (`parse_claude_result` or `parse_codex_result`).
8. persist run status.
9. fire optional callback `(title, result_text, status)`.
10. reschedule next run.

## Status Codes

Typical status values:

- `success`
- `error:folder_missing`
- `error:cli_not_found_claude`
- `error:cli_not_found_codex`
- `error:timeout`
- `error:exit_<code>`

## `execution.py` Command Rules

`build_cmd(provider, model, prompt, permission_mode)`:

- Claude:
  - `claude -p <prompt> --output-format json --model <model> --permission-mode <mode> --no-session-persistence`
- Codex:
  - `codex exec --json --color never --skip-git-repo-check ...`
  - `bypassPermissions` -> `--dangerously-bypass-approvals-and-sandbox`
  - otherwise -> `--full-auto`

## Timezone Handling

Cron expressions are interpreted in the user's timezone, not UTC.

Resolution order per job:

1. `CronJob.timezone` (per-job override, usually empty).
2. `AgentConfig.user_timezone` (global config).
3. Host system timezone (via `$TZ` or `/etc/localtime`).
4. `UTC` (final fallback).

Implementation: `_schedule_job()` converts `datetime.now()` to the resolved timezone, passes the naive local time to `CronSim`, then re-attaches the timezone to compute the correct UTC delay.

Workspace tools: `cron_add.py` and `cron_edit.py` accept `--timezone`. `cron_time.py` shows current time in configured and common timezones. `cron_list.py` reports the active `user_timezone`.

## Design Choice

- No system crontab integration.
- Scheduling is fully in-process and tied to bot lifecycle.
- `cron_jobs.json` is the single source of truth.
