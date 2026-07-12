# cron/

In-process cron scheduling with JSON persistence and one-shot CLI execution.

## Files

- `manager.py`: `CronJob`, `CronManager` CRUD/persistence
- `observer.py`: `CronObserver` scheduling, watcher, execution pipeline
- `execution.py`: provider command builders, result parsing, one-shot subprocess helper
- `dependency_queue.py`: shared dependency locks (cron + webhook cron_task)
- `infra/task_runner.py` (shared): folder checks + one-shot task execution for cron/webhook/background

## Cron job model

Core fields:

- `id`, `title`, `description`, `schedule`
- `task_folder`, `agent_instruction`, `enabled`
- `timezone` (optional per-job IANA override)
- `created_at`, `last_run_at`, `last_run_status`

Routing fields:

- `chat_id` (default `0`) -- target chat for result delivery
- `topic_id` (default `None`) -- optional forum topic within target chat
- `transport` (default `"tg"`) -- transport identifier (`"tg"` or `"mx"`)

Routing fields are editable in place via the `cron_edit` tool
(`--chat-id`, `--topic-id`, `--transport`, `--clear-topic-id`) to redirect an
existing job to a different chat or topic.

Execution overrides:

- `provider`
- `model`
- `reasoning_effort`
- `cli_parameters`

Scheduling guards:

- `quiet_start`, `quiet_end`
- `dependency`

Delivery:

- `silent_on_success` (default `false`) -- suppress result delivery when the run succeeds; errors are still delivered

## Persistence

File: `~/.ductor/cron_jobs.json`

- format: `{ "jobs": [...] }`
- atomic writes via temp+replace

## Observer lifecycle

`start()`:

1. schedule enabled jobs
2. start mtime watcher loop

Watcher:

- polls file mtime every 5s
- on change: reload + full reschedule

`reschedule_now()` is used by interactive cron toggles and updates mtime baseline first to avoid watcher race.

## Execution path

When a job fires:

1. acquire dependency lock when configured
2. quiet-hour gate (only when `job.quiet_*` is set; no fallback to global heartbeat quiet hours)
3. resolve/validate task folder (`workspace/cron_tasks/<task_folder>`)
4. resolve `TaskExecutionConfig` via `resolve_cli_config(...)`
5. enrich prompt with `<task_folder>_MEMORY.md` instructions
6. build provider command (`build_cmd`)
7. execute one-shot subprocess with timeout
8. parse provider output
9. invoke optional result callback when the execution path reaches callback emission
10. update run status (`last_run_status`, `last_run_at`)
11. schedule next occurrence

## Command builders (`execution.py`)

Supported providers:

- Claude
- Codex
- Gemini

Antigravity is not supported in the cron one-shot command builder. Use a supported provider override for cron jobs when the global chat provider is `antigravity`.

Examples:

- Claude: `claude -p --output-format json ... --no-session-persistence -- <prompt>`
- Codex: `codex exec --json ... -- <prompt>`
- Gemini: `gemini -p "" --output-format json --include-directories . ...` (prompt passed via stdin)

`bypassPermissions` behavior:

- Codex: `--dangerously-bypass-approvals-and-sandbox`
- Gemini: `--approval-mode yolo`

## Status values

Typical values:

- `success`
- `error:folder_missing`
- `error:cli_not_found_claude`
- `error:cli_not_found_codex`
- `error:cli_not_found_gemini`
- `error:timeout`
- `error:exit_<code>`

Quiet-hour skips are silent:

- no `last_run_status` update
- no result callback

Folder-missing nuance:

- `error:folder_missing` updates `last_run_status`
- no result callback is emitted for that path

Silent-on-success nuance:

- when `silent_on_success` is set and the run succeeds, `last_run_status` is updated but no result callback is emitted
- failures (any `error:*` status) are always delivered

## Result routing

Cron results are delivered through `MessageBus` using `Envelope` objects built by `bus/adapters.py::cron_result_envelope(...)`.

- **UNICAST**: when `chat_id` is non-zero, the result is delivered to that specific chat/topic on the matching transport.
- **BROADCAST**: when `chat_id` is `0` (default), the result is broadcast to all authorized users.

Fallback behavior (Telegram):

- if unicast delivery fails (e.g. bot removed from group, topic deleted), the result falls back to the main user's private chat (`allowed_user_ids[0]`) with an explanation of the delivery failure.

Fallback behavior (Matrix):

- if the target room cannot be resolved, the result falls back to broadcast across all allowed rooms.

Delivery tracking (#160):

- execution status and delivery status are tracked separately: `last_delivery_status` (`ok` / `failed` / `skipped`), `last_delivery_error`, and — only on delivery failure — the full `last_result_text` are persisted per job in `cron_jobs.json`, so an undelivered result can be resent without re-running the job.
- `/cron` marks affected jobs with `(delivery failed)`; `cron_list.py` exposes the preserved fields to the agent.

## Environment variables

The CLI subprocess receives routing context via environment variables:

- `DUCTOR_CHAT_ID` -- current chat ID (set when `chat_id` is non-zero)
- `DUCTOR_TOPIC_ID` -- current topic ID (set when `topic_id` is non-None)
- `DUCTOR_TRANSPORT` -- transport identifier (`"tg"` or `"mx"`)

These are injected by `_build_subprocess_env()` (host mode) and `docker_wrap()` (container mode).

The `cron_add.py` tool script auto-reads these env vars to populate the job's routing fields, so jobs created from within a chat/topic automatically route results back to that location.

## Timezone resolution

Per-job scheduling resolution:

1. `CronJob.timezone`
2. global `user_timezone`
3. host timezone
4. UTC fallback

Cron expressions are evaluated in resolved local wall-clock time.

## Dependency queue

Shared queue key behavior:

- same dependency key -> FIFO serialization
- different/no key -> parallel execution
- shared with webhook `cron_task` runs

## Telegram interaction

`/cron` uses interactive selector (`crn:*` callbacks):

- paging
- refresh
- per-job enable/disable
- bulk all-on/all-off
