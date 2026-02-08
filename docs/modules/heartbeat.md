# heartbeat/

Periodic background heartbeat loop per allowed user chat.

## Files

- `observer.py`: `HeartbeatObserver`, quiet-hour logic, loop lifecycle.
- `__init__.py`: exports `HeartbeatObserver`.

## Purpose

Heartbeat runs periodic model turns in the existing main session to produce proactive alerts without opening new sessions.

## Public API (`HeartbeatObserver`)

- `set_result_handler(handler)`
- `set_heartbeat_handler(handler)`
- `set_busy_check(check)`
- `set_stale_cleanup(cleanup)` -- callback to kill wall-clock-stale CLI processes (wired to `ProcessRegistry.kill_stale`)
- `start()`
- `stop()`

Helper:

- `is_quiet_hour(now_hour, quiet_start, quiet_end)` (supports wrap-around windows, evaluated in `user_timezone`)

## Lifecycle

`start()` checks:

1. `config.heartbeat.enabled` must be true,
2. heartbeat handler must be set.

Then it starts background `_loop()`.

`_loop()`:

1. sleep `interval_minutes`,
2. skip if disabled/stopped,
3. run `_tick()`.

`_tick()`:

- skip full cycle during quiet hours (evaluated in `user_timezone`, fallback UTC),
- iterate all `allowed_user_ids` and call `_run_for_chat(chat_id)`.

`_run_for_chat()`:

1. skip if busy check says chat has active process,
2. call heartbeat handler (orchestrator),
3. if returned text is not `None`, deliver via result handler.

## Orchestrator Contract

Observer does not talk to CLI directly. It delegates to `Orchestrator.handle_heartbeat()` / `heartbeat_flow()` for session logic:

- uses read-only `SessionManager.get_active()` (never creates or destroys sessions),
- requires existing `session_id`,
- skips on provider mismatch (session provider != current config provider),
- enforces cooldown from `session.last_active`,
- suppresses `ack_token` responses,
- updates session metrics only for actual alert responses.

## Resilience

The heartbeat loop includes several defenses against system suspend and crashes:

- **Wall-clock gap detection**: compares `time.time()` between ticks to detect system suspend (where `asyncio.sleep` pauses but wall clock advances). Logs a warning on gaps > 2x interval.
- **Stale process cleanup**: before each tick, calls `stale_cleanup` callback to kill CLI processes that exceeded `cli_timeout * 2` in wall-clock time. Catches processes that survived suspend where monotonic-based `asyncio.timeout` did not fire.
- **Catch-all exception handler**: each `_tick()` call is wrapped in a broad `except Exception` so a single tick failure does not kill the loop.
- **Task crash callback**: `_log_task_crash` is attached via `add_done_callback` to detect and log unexpected background task termination.

## Delivery Logging

`TelegramBot._on_heartbeat_result()` logs both before and after sending to Telegram (`DEBUG` on entry, `INFO` on successful delivery) for end-to-end observability.
