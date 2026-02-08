# session/

Per-chat session lifecycle with JSON persistence.

## Files

- `manager.py`: `SessionData`, `SessionManager`

## `SessionData`

Fields:

- `session_id`
- `chat_id`
- `provider`
- `created_at`, `last_active` (ISO UTC)
- `message_count`
- `total_cost_usd`
- `total_tokens`

`__post_init__` auto-fills timestamps if missing.

## `SessionManager` API

- `resolve_session(chat_id, provider=None) -> (SessionData, is_new)`
- `get_active(chat_id) -> SessionData | None`
- `reset_session(chat_id) -> SessionData`
- `update_session(session, cost_usd=0.0, tokens=0)`

## Freshness Rules (`_is_fresh`)

Session is stale if any condition matches:

- `max_session_messages` reached,
- idle timeout exceeded (skipped when `idle_timeout_minutes` is `0`),
- daily reset boundary crossed (`daily_reset_hour`, evaluated in `user_timezone` via `resolve_user_timezone()`),
- invalid `last_active` timestamp.

Each freshness check is logged at `DEBUG` level with `reason=` for diagnostics.

When `idle_timeout_minutes` is `0`, sessions never expire due to inactivity. Only an explicit `/new` command or provider switch resets them.

## Provider Switch Behavior

If `resolve_session()` is called with a different provider than the stored session:

- existing session is treated as new provider context (`is_new=True`),
- on reusable sessions, `session_id` is cleared,
- `provider` is updated,
- `message_count` is reset to `0`.

## Persistence

File: `~/.ductor/sessions.json` (dict keyed by chat ID as string).

- load: tolerant to missing/corrupt JSON (returns empty dict).
- save: atomic temp-file write + replace.
- all file I/O runs in `asyncio.to_thread()`.
