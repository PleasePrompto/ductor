# session/

Per-chat session lifecycle with JSON persistence and provider-isolated state.

## Files

- `manager.py`: `ProviderSessionData`, `SessionData`, `SessionManager`

## Data Model

### `ProviderSessionData`

Provider-local bucket:

- `session_id`
- `message_count`
- `total_cost_usd`
- `total_tokens`

### `SessionData`

Chat-level envelope:

- `chat_id`
- `provider` (currently active provider)
- `model` (currently active model)
- `created_at`, `last_active` (ISO UTC)
- `provider_sessions: dict[str, ProviderSessionData]`

Compatibility behavior:

- constructor still accepts legacy flat fields (`session_id`, `message_count`, `total_cost_usd`, `total_tokens`) for old JSON/tests,
- if legacy fields are provided and `provider_sessions` is missing, they are migrated into the current provider bucket.

Compatibility properties:

- `session_id`
- `message_count`
- `total_cost_usd`
- `total_tokens`

These read/write `provider_sessions[self.provider]`, so existing call sites remain unchanged while data is isolated per provider.

Utility methods:

- `clear_all_sessions()`: removes all provider buckets
- `clear_provider_session(provider)`: removes one provider bucket

## `SessionManager` API

- `resolve_session(chat_id, provider=None, model=None) -> (SessionData, is_new)`
- `get_active(chat_id) -> SessionData | None`
- `reset_session(chat_id, provider=None, model=None) -> SessionData`
- `reset_provider_session(chat_id, provider, model) -> SessionData`
- `update_session(session, cost_usd=0.0, tokens=0) -> None`
- `sync_session_target(session, provider=None, model=None) -> None`

Behavior highlights:

- `reset_session(...)` is a full low-level reset: new `SessionData` with empty `provider_sessions`.
- `reset_provider_session(...)` clears only one provider bucket and keeps other providers intact.
- `update_session(...)` merges provider buckets from caller state into persisted state, then increments counters only for the active provider.
- merge logic prevents stale snapshots from regressing counters (`max(existing, incoming)` per metric).

## Freshness Rules (`_is_fresh`)

A session is stale if any condition matches:

- `max_session_messages` reached (uses active provider `message_count`)
- idle timeout exceeded (`idle_timeout_minutes`; `0` disables idle expiry)
- daily reset boundary crossed (`daily_reset_enabled=true`, hour=`daily_reset_hour`, timezone=`user_timezone`)
- invalid `last_active` timestamp

Each decision is logged at `DEBUG` with `reason=...`.

## Provider Switch Behavior

`resolve_session()` no longer clears IDs/counters on provider switch.

- it updates `session.provider` and `session.model`,
- returns `is_new=True` only when the target provider has no `session_id`,
- switching back to a previously used provider resumes that provider’s original session and metrics.

## Error Behavior

CLI errors do not reset sessions in `SessionManager`.

- chat-command resets are provider-targeted (`reset_provider_session`, used by `/new` and SIGKILL recovery),
- normal runtime errors preserve session state so the next user message can resume.

## Persistence

File: `~/.ductor/sessions.json` (dict keyed by chat ID string).

- load: tolerant to missing/corrupt JSON (returns `{}`),
- save: atomic temp write + replace,
- I/O runs in `asyncio.to_thread()`,
- `dataclasses.asdict(SessionData)` serializes `provider_sessions` as nested JSON.
