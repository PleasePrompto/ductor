# orchestrator/

Routing layer between Telegram bot UI and CLI providers. Owns command dispatch, directive parsing, session flow orchestration, hooks, model switching, and cron/heartbeat/webhook wiring.

## Files

- `core.py`: `Orchestrator` lifecycle, routing, error boundary.
- `registry.py`: `CommandRegistry`, `OrchestratorResult`.
- `commands.py`: slash command handlers.
- `flows.py`: `normal`, `normal_streaming`, `heartbeat_flow`.
- `directives.py`: leading `@...` parser.
- `hooks.py`: hook model and built-in `MAINMEMORY_REMINDER` hook.
- `model_selector.py`: `/model` wizard callbacks and model switch logic.

## Creation (`Orchestrator.create`)

1. Resolve paths from configured `ductor_home`.
2. `init_workspace(paths)` in thread.
3. Set `DUCTOR_HOME` env var.
4. Check provider auth (`check_all_auth`).
5. Set authenticated providers in `CLIService`.
6. Start `CronObserver`.
7. Start `HeartbeatObserver`.
8. Start `WebhookObserver`.
9. Start rule-sync task (`watch_rule_files(paths.workspace)`).

## Routing Entry Points

- `handle_message(chat_id, text)` (non-streaming)
- `handle_message_streaming(chat_id, text, on_text_delta, on_tool_activity, on_system_status)`

Both flow through `_handle_message_impl()`:

1. clear abort flag (`ProcessRegistry.clear_abort`),
2. scan input for suspicious patterns (log-only),
3. route via `_route_message()`,
4. catch domain/infrastructure exceptions and return generic internal error text.

## Command Dispatch

Registered commands:

- `/new`
- `/stop`
- `/status`
- `/model`
- `/model ` (prefix form, supports `/model <name>`)
- `/memory`
- `/cron`
- `/upgrade`
- `/diagnose`

`CommandRegistry` supports exact and prefix matching (`name.endswith(" ")`).

## Directives

`parse_directives(text, known_models)` consumes only the beginning of a message.

- model directive: `@<model-id>` if `<model-id>` is in `known_models`.
- raw directives: any other leading `@key` or `@key=value`.

Current orchestrator behavior:

- `known_models` is `_CLAUDE_MODELS` (`haiku`, `sonnet`, `opus`).
- Codex IDs are not recognized as inline model directives.

If a message is only a model directive (`@sonnet` with no prompt text), orchestrator returns instructional text instead of executing.

## Normal Flow (`flows.py`)

`_prepare_normal()`:

1. resolve requested model/provider,
2. resolve session for provider,
3. new session -> append `MAINMEMORY.md` as `append_system_prompt`,
4. apply hooks (`MessageHookRegistry.apply`),
5. build `AgentRequest`.

`normal()`:

- run `CLIService.execute()`.
- retry once only when resume call fails (`response.is_error` and `request.resume_session`): reset session and retry fresh.
- on final error: kill processes + reset session.

`normal_streaming()`:

- run `CLIService.execute_streaming()` with `on_system_status` callback for compaction display.
- same resume-failure retry behavior as `normal()`.
- on final error: kill processes + reset session.

On success both paths call `_finish_normal()`:

- update stored session ID when provider returns a new one,
- increment message count and usage metrics,
- append session age warning when session exceeds `session_age_warning_hours` and message count is a multiple of 10.

## Message Hooks

Built-in hook: `MAINMEMORY_REMINDER`.

- condition: every 6th outgoing message (`message_count + 1`).
- action: appends a memory-check suffix to prompt.

## Heartbeat Flow

`heartbeat_flow(orch, chat_id)`:

1. read-only session check via `SessionManager.get_active()` (never creates/destroys sessions),
2. skip if no active session or no `session_id`,
3. skip on provider mismatch (session provider != current config provider),
4. skip when user activity is within cooldown window,
5. send heartbeat prompt via `CLIService.execute()` with `resume_session`,
6. strip configured ACK token,
7. ACK-only -> return `None` (suppressed),
8. non-ACK -> update session metrics and return alert text.

## Model Selector (`model_selector.py`)

Wizard callback namespace: `ms:`.

- provider step: `ms:p:<provider>`
- model step: `ms:m:<model_id>`
- reasoning step (Codex): `ms:r:<effort>:<model_id>`
- back: `ms:b:root` or `ms:b:<provider>`

`switch_model()` behavior:

- if model changes: kill active processes + reset session,
- if only reasoning effort changes on same Codex model: no reset, only config update,
- update in-memory config + CLIService default model,
- when provider changes: update config provider,
- when effort provided: update `reasoning_effort`,
- persist changes to `config.json`.

## Webhook Wiring

`Orchestrator` owns webhook observer wiring:

- `set_webhook_result_handler(handler)` -> forwards `WebhookResult`.
- `set_webhook_wake_handler(handler)` -> injects bot-layer wake handler.

Wake execution itself stays in bot layer (`TelegramBot._handle_webhook_wake`) so it can reuse the same per-chat lock as normal chat updates.

## Upgrade Command (`cmd_upgrade`)

`/upgrade` Telegram command flow:

1. Calls `check_pypi()` for latest version info.
2. If unreachable: returns error message.
3. If already up to date: shows installed/latest versions.
4. If update available: shows version diff + package summary + inline keyboard (`upg:yes:<version>` / `upg:no`).

Callback handling is in `TelegramBot` (bot layer), not orchestrator.

## Shutdown

`Orchestrator.shutdown()`:

1. cancel and await rule-sync task,
2. stop heartbeat observer,
3. stop webhook observer,
4. stop cron observer.
