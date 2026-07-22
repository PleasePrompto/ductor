# Automation Quickstart

ductor automation systems:

| System | Trigger | Execution Context | Output |
|---|---|---|---|
| Sessions (`/session`) | user command | named session (persistent) | chat notification |
| Delegated tasks (`TaskHub`) | task tool scripts (`/tasks/*` API) | shared background task runtime | chat notification + parent-session injection |
| Cron jobs | schedule | isolated task folder | chat result |
| Webhooks | HTTP POST | wake or isolated `cron_task` | chat result |
| Heartbeat | interval | active main session | chat alert (non-ACK only) |
| Cleanup | daily hour | filesystem maintenance | no chat message |

## Named sessions (`/session`)

`/session <prompt>` starts a named background session. The chat is free immediately; a message is sent when the task completes. Sessions persist and support follow-ups.

Key properties:

- auto-generates memorable compact names (e.g. `swiftfox`, `tallnewt`)
- supports provider isolation: `/session @codex <prompt>`
- follow-up in foreground: `@session-name <message>`
- follow-up in background: `/session @session-name <message>`
- session management: `/sessions` (list, end, end all)
- `/stop` cancels all sessions for the chat
- `/stop_all` on the main agent also aborts active work across other agents (sub-agent fallback is local-only)
- max 5 concurrent tasks, max 10 user-created sessions per chat
- `/status` shows active background tasks
- `/session` background timeout uses `config.timeouts.background`

Inter-agent sessions use deterministic scoped names (`ia.<sender-slug>.t<topic>.x<hash>`, or legacy `ia-<sender>` without source context) and are not created through `/session`. They live in the same `named_sessions.json` registry but are capped separately: max 32 per chat with oldest-idle eviction, and they never count against the 10-session user quota above.

Restart behavior:

- active asyncio task objects are lost on restart,
- persisted named sessions can still be resumed,
- startup recovery automatically retries safe named sessions that were `running` before restart when Telegram is the primary transport
- Matrix-primary startup currently has no equivalent auto-recovery pipeline

Status values for named-session runs: `ok`, `error:timeout`, `error:cli`, `error:internal`, `aborted`.

## Delegated tasks (`TaskHub`, `/tasks`)

Delegated tasks are separate from `/session`:

- persisted in `~/.ductor/tasks.json`
- task folders in `~/.ductor/workspace/tasks/<task_id>/`
- managed via `/tasks` command (running/waiting/finished + cancel/cleanup controls)
- created/resumed/cancelled/deleted through task tools (`tools/task_tools/*.py`) over `InternalAgentAPI /tasks/*`
- timeout source: `config.tasks.timeout_seconds`

Result flow:

- task completion/failure is posted to the chat
- result is injected into parent agent's current active session (`handle_task_result`)
- task questions (`ask_parent.py`) are posted and injected via `handle_task_question`
- forum-topic tasks route back to the originating topic via `thread_id` / `DUCTOR_TOPIC_ID`

## Cron jobs

Cron jobs run in `~/.ductor/workspace/cron_tasks/<task_folder>/`.

Each run is a fresh one-shot subprocess in the task folder. It does not reuse the main chat session.

Typical task folder:

```text
~/.ductor/workspace/cron_tasks/weather-report/
  CLAUDE.md
  AGENTS.md
  TASK_DESCRIPTION.md
  weather-report_MEMORY.md
  scripts/
```

Rule files are kept in sync automatically (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`) based on newest mtime per directory.

Rule-file sync behavior (all workspace directories, recursive):

- files: `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`
- source of truth per directory: newest file by mtime
- sync runs at init (`sync_rule_files`) and continuously (`watch_rule_files`, every 10s)
- result: edits are propagated to older existing sibling rule files
- missing siblings are generally not auto-created, except cron task folders where `ensure_task_rule_files(...)` backfills missing provider rule files

Runtime behavior:

1. dependency lock (`dependency_queue`)
2. quiet-hour check (only when `job.quiet_*` is set; no fallback to global heartbeat quiet hours)
3. optional preflight gate (`cron_preflight.enabled`): runs `cron_tasks/<task_folder>/scripts/preflight.py`; when it exits `0` and prints `skip_marker` as its last line, the agent run is skipped (status `success:preflight`). Fail-open otherwise.
4. folder check
5. resolve task overrides (`provider/model/reasoning/cli_parameters`)
6. build provider command (`claude`, `codex`, `gemini`, or `grok`)
7. execute with timeout (`cli_timeout`)
8. parse output
9. send result callback to chat when the run actually executes and produces a callback path
   - routing: UNICAST to originating chat when the job has `chat_id` set; BROADCAST to all users when `chat_id` is zero/unset
   - if UNICAST delivery fails (e.g. transport unavailable), the bus cascading fallback delivers to an available transport with an explanation
10. persist status (`last_run_status`, `last_run_at`); execution and delivery status are tracked separately, and a failed delivery preserves `last_result_text` for a later resend

Cron and webhook `cron_task` one-shot execution does not support Antigravity. Set a supported provider override when the global chat provider is `antigravity`.

Per-job override fields in `cron_jobs.json`:

```json
{
  "provider": "gemini",
  "model": "gemini-2.5-pro",
  "reasoning_effort": null,
  "cli_parameters": ["--debug"],
  "quiet_start": 22,
  "quiet_end": 7,
  "dependency": "nightly-reports",
  "silent_on_success": true
}
```

Notes:

- `reasoning_effort` is only used for Codex models that support it.
- task `cli_parameters` are appended after the global provider-specific args, so task-level flags win when the underlying CLI accepts repeated options.
- cron status includes `error:cli_not_found_<provider>` for missing provider binaries.
- `error:folder_missing` updates `last_run_status` but does not emit a result callback.
- quiet-hour skips do not emit result callbacks and do not update `last_run_status`.
- `silent_on_success` (default `false`) suppresses the result callback when the run succeeds (status `success`) but still updates `last_run_status`; failures are always delivered.
- routing fields (`chat_id`, `topic_id`, `transport`) can be redirected on an existing job via the `cron_edit` tool (`--chat-id`, `--topic-id`, `--transport`, `--clear-topic-id`); `cron_add` seeds them from the creating chat/topic.
- when `cron_delivery_retry.enabled` is set, a background sweep periodically resends preserved delivery failures without re-running the agent (at-least-once, `interval_seconds` apart, up to `max_attempts`); jobs mid-execution are skipped so a retry never races a fresh run.

## Webhooks

Server route: `POST /hooks/{hook_id}`

Validation order:

1. rate limit
2. content type (`application/json`)
3. JSON object body
4. hook exists
5. hook enabled
6. auth (`bearer` or `hmac`)
7. accept and dispatch asynchronously (`202`)

Modes:

- `wake`: inject rendered prompt into active Telegram chat flow
- `cron_task`: run isolated one-shot execution in task folder

Prompt payload is wrapped with safety markers before execution.

`cron_task` mode supports the same override/quiet/dependency fields as cron jobs.

Execution overrides (`provider`, `model`, `reasoning_effort`, `cli_parameters`) apply only to `mode=cron_task`. `wake` resumes the live chat session with its current provider/model, so the `webhook_add` / `webhook_edit` tools reject those flags on a wake hook (they were silently ignored before). A pre-existing wake hook that still carries overrides logs a warning and ignores them at dispatch.

Quiet-hour behavior in `cron_task` mode:

- only `hook.quiet_start` / `hook.quiet_end` are considered
- no fallback to global heartbeat quiet hours

Typical status values:

- `success`
- `error:no_response`
- `error:no_task_folder`
- `error:folder_missing`
- `error:cli_not_found_claude`
- `error:cli_not_found_codex`
- `error:cli_not_found_gemini`
- `error:timeout`
- `error:exit_<code>`
- `skipped:quiet_hours`

Current transport limitation:

- webhook `wake` depends on a configured wake handler and is currently wired by Telegram startup only
- Matrix-primary setups do not provide that handler right now, so `wake` returns `error:no_wake_handler`
- for Matrix-only deployments, use `cron_task` mode instead of `wake`

## Heartbeat

Heartbeat runs only when `heartbeat.enabled=true`.

Observer behavior:

- interval loop (`interval_minutes`)
- quiet-hour suppression in `user_timezone`
- busy-chat skip via `ProcessRegistry.has_active`
- stale process cleanup hook before each tick

`heartbeat_flow` behavior:

- uses read-only active session lookup,
- skips if no session or no provider-compatible session,
- enforces cooldown via `last_active`,
- sends heartbeat prompt with `resume_session`,
- suppresses pure ACK token responses,
- updates session metrics only for non-ACK alerts.

Default ACK token: `HEARTBEAT_OK`.

Default prompt asks the model to review memory + cron context and either send something useful or respond exactly with `HEARTBEAT_OK`.

### Group targets

`group_targets` allows heartbeat to run in specific group chats/topics in addition to private user chats. Each target supports per-target overrides for prompt, ack token, interval, and quiet hours.

```json
{
  "heartbeat": {
    "enabled": true,
    "interval_minutes": 30,
    "quiet_start": 21,
    "quiet_end": 8,
    "group_targets": [
      {
        "chat_id": -1001234567890,
        "topic_id": 42,
        "interval_minutes": 60,
        "prompt": "Check project status and report if anything needs attention.",
        "quiet_start": 23,
        "quiet_end": 6
      },
      {
        "chat_id": -1009876543210
      }
    ]
  }
}
```

Per-target override fields (all optional, fall back to global heartbeat values when unset):

- `prompt`
- `ack_token`
- `interval_minutes`
- `quiet_start` / `quiet_end`

Lifecycle note:

- heartbeat/cleanup config values hot-reload
- observer start/stop does not hot-toggle
- if disabled at startup, changing `enabled` to `true` requires restart

## Cleanup

Cleanup runs once per day at `cleanup.check_hour` (in `user_timezone`), checked hourly.

Deletes old files (recursive) from:

- `workspace/telegram_files/` (Telegram media)
- `workspace/matrix_files/` (Matrix media)
- `workspace/output_to_user/`
- `workspace/api_files/`

Retention windows:

- `cleanup.media_files_days`
- `cleanup.output_to_user_days`
- `cleanup.api_files_days`

Cleanup also prunes empty subdirectories after deletion, so dated upload folders are removed once empty.

## Config blocks

```json
{
  "heartbeat": {
    "enabled": false,
    "interval_minutes": 30,
    "cooldown_minutes": 5,
    "quiet_start": 21,
    "quiet_end": 8,
    "ack_token": "HEARTBEAT_OK"
  },
  "cleanup": {
    "enabled": true,
    "media_files_days": 30,
    "output_to_user_days": 30,
    "api_files_days": 30,
    "check_hour": 3
  },
  "webhooks": {
    "enabled": false,
    "host": "127.0.0.1",
    "port": 8742,
    "rate_limit_per_minute": 30
  }
}
```

Cron jobs and webhook entries are stored in `cron_jobs.json` / `webhooks.json`.
