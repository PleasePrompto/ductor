# webhook/

HTTP ingress for external event triggers.

## Files

- `models.py`: `WebhookEntry`, `WebhookResult`, `render_template`
- `manager.py`: persistence CRUD for hooks
- `auth.py`: bearer/hmac validation + rate limiter
- `server.py`: aiohttp server and request validation chain
- `observer.py`: server lifecycle, dispatch logic, watcher
- `infra/task_runner.py` (shared): folder checks + one-shot task execution for webhook/cron/background

## Persistence

File: `~/.ductor/webhooks.json`

- format: `{ "hooks": [...] }`
- atomic writes
- mtime watcher reloads hooks every 5s on changes

## Hook model highlights

Core fields:

- `id`, `title`, `mode`, `prompt_template`, `enabled`
- `task_folder` (`cron_task` mode)
- trigger/error telemetry fields

Auth fields:

- `auth_mode` (`bearer` or `hmac`)
- per-hook token/secret and hmac config options

Bearer fallback behavior:

- for `auth_mode="bearer"`, validation uses `hook.token` when set
- otherwise it falls back to global `webhooks.token`

Execution overrides (`cron_task` only):

- `provider`, `model`, `reasoning_effort`, `cli_parameters`
- `quiet_start`, `quiet_end`, `dependency`

Execution overrides apply only to `cron_task`. A `wake` hook resumes the live chat session with its current provider/model, so `webhook_add` / `webhook_edit` reject `--provider` / `--model` / `--reasoning-effort` / `--cli-parameters` on a wake hook (via `reject_wake_overrides`). A legacy wake hook that still carries overrides logs a warning at dispatch and ignores them.

Quiet-hour note:

- `cron_task` quiet hours are evaluated only from hook-level `quiet_start` / `quiet_end`.
- no fallback to global heartbeat quiet hours.

Template rendering (`render_template`):

- placeholder syntax: `{{field}}`
- source: top-level keys in incoming JSON payload
- missing key behavior: rendered as `{{?field}}` (visible but non-fatal)
- rendering never raises for missing placeholders

## Server routes

- `GET /health`
- `POST /hooks/{hook_id}`

Validation order for POST:

1. rate limit
2. content type
3. JSON object body
4. hook exists
5. hook enabled
6. auth validation
7. dispatch async (`202` response immediately)

## Observer startup

When `webhooks.enabled=true`:

1. auto-generate global webhook token if empty and persist to config
2. start server
3. start watcher loop

## Dispatch flow

`WebhookObserver._dispatch(hook_id, payload)`:

1. lookup hook
2. render template
3. wrap rendered prompt with external-input safety markers
4. route by mode:
   - `wake`
   - `cron_task`
5. record trigger + error status
6. invoke optional result callback

## Mode: `wake`

- observer calls wake handler for each `allowed_user_id`
- bot wake handler acquires per-chat lock and routes through normal orchestrator message flow
- result status is `success` only if at least one non-empty response is produced

Current transport limitation:

- `wake` is currently wired by Telegram startup
- Matrix startup does not currently provide a wake handler, so webhook `wake` on Matrix-only setups returns `error:no_wake_handler`

## Mode: `cron_task`

One-shot isolated run in task folder:

1. validate `task_folder`
2. quiet-hour gate (hook-level only; no heartbeat quiet-hour fallback)
3. dependency lock
4. resolve task execution config
5. build provider command (Claude/Codex/Gemini/Grok)
6. execute with timeout
7. parse result and return `WebhookResult`

Antigravity is not supported in webhook `cron_task` mode because that path uses the cron one-shot command builder.

## Common result statuses

- `success`
- `error:not_found`
- `error:no_wake_handler`
- `error:no_response`
- `error:no_task_folder`
- `error:folder_missing`
- `error:cli_not_found_claude`
- `error:cli_not_found_codex`
- `error:cli_not_found_gemini`
- `error:cli_not_found_grok`
- `error:timeout`
- `error:exit_<code>`
- `skipped:quiet_hours`
- `error:unknown_mode_<mode>`
- `error:exception`

## Security notes

- default bind: `127.0.0.1:8742`
- per-hook bearer/hmac auth
- request body size limit (`max_body_bytes`)
- sliding-window rate limit
- external payload boundary markers injected into prompt
