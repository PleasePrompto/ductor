# session/

Session lifecycle and persistence with provider isolation and topic/channel-aware keys.

## Files

- `session/key.py`: `SessionKey(transport, chat_id, topic_id)`
- `session/manager.py`: `ProviderSessionData`, `SessionData`, `SessionManager`
- `session/named.py`: `NamedSession`, `NamedSessionRegistry`

## Session identity (`SessionKey`)

`SessionKey` is transport-aware:

- Telegram default chats -> `SessionKey("tg", chat_id, None)`
- Telegram forum topics -> `SessionKey("tg", chat_id, message_thread_id)`
- Matrix rooms -> `SessionKey("mx", mapped_room_int, None)`
- API channel scope -> `SessionKey("api", chat_id, channel_id)`

Persistence key (`storage_key`) format:

- legacy accepted on parse: `"<chat_id>"` or `"<chat_id>:<topic_id>"`
- current: `"<transport>:<chat_id>"` or `"<transport>:<chat_id>:<topic_id>"`

Parsing is backward-compatible (`SessionKey.parse`).

## `SessionData` model

Fields:

- `chat_id`
- `topic_id` (optional)
- `topic_name` (optional cached display name)
- `provider`, `model` (active target for this session key)
- `created_at`, `last_active`
- `provider_sessions: dict[str, ProviderSessionData]`

Provider bucket (`ProviderSessionData`):

- `session_id`
- `message_count`
- `total_cost_usd`
- `total_tokens`

Compatibility behavior:

- legacy flat metrics/session fields are migrated into provider buckets
- legacy storage keys are still accepted

## `SessionManager` API

- `resolve_session(key, provider=None, model=None, preserve_existing_target=False)`
- `get_active(key)`
- `list_active_for_chat(chat_id)`
- `list_all()`
- `reset_session(key, provider=None, model=None)`
- `reset_provider_session(key, provider, model)`
- `update_session(session, cost_usd=0.0, tokens=0)`
- `sync_session_target(session, provider=None, model=None)`
- `set_topic_name_resolver(resolver)`

## Freshness and rollover

Session freshness checks include:

- `max_session_messages`
- idle timeout (`idle_timeout_minutes`, `0` disables)
- daily reset boundary (`daily_reset_enabled`, `daily_reset_hour`, `user_timezone`)
- timestamp validity

Stale sessions are replaced on next `resolve_session(...)` call.

## Provider/model switching behavior

Switching model/provider for a key:

- updates active target for that key
- keeps other provider buckets intact
- `is_new=True` only if target provider bucket lacks `session_id`

This enables seamless return to previously used provider buckets.

Consumer-facing nuance:

- `/model` changes the active target for the current `SessionKey`
- `/new` does not wipe every bucket; the orchestrator resets only the bucket for the provider currently resolved from `config.model`
- temporary per-turn directives do not change what `/new` resets later unless the session target itself was updated

## Topic name integration

`SessionManager` can resolve and backfill topic names through a callback:

- `set_topic_name_resolver((chat_id, topic_id) -> str)`
- used by bot startup with `TopicNameCache`
- persisted `topic_name` improves `/status` and `/sessions` readability

## Named sessions (`NamedSessionRegistry`)

Purpose:

- background `/session` registry
- deterministic inter-agent sessions (`ia.<sender-slug>.t<topic>.x<hash>`, or legacy `ia-<sender>` without source context)

Model fields:

- `name`, `chat_id`, `provider`, `model`
- `session_id`, `prompt_preview`, `status`, `created_at`, `message_count`, `last_prompt`

Status values:

- `running`
- `idle`
- `ended`

Behavior:

- user-created cap: `MAX_SESSIONS_PER_CHAT = 10` (only `/session`-created names count; the `ia-`/`ia.` namespace is excluded via `active_names`)
- inter-agent cap: `MAX_INTERAGENT_SESSIONS_PER_CHAT = 32`, enforced in `add()` with oldest-idle eviction (running scoped sessions are never evicted); scoped sessions bypass `create()` and its quota
- persisted `running` entries are downgraded to `idle` on load
- recovered-running sessions are tracked for startup recovery (inter-agent names are excluded from recovery)
- inter-agent conversations use deterministic scoped names `ia.<sender-slug>.t<topic>.x<hash>`, or legacy `ia-<sender>` without source context
- stale CLI session IDs on those named sessions are retried once with a fresh session after update/cache-clear style failures

## Persistence

- sessions: `~/.ductor/sessions.json`
- named sessions: `~/.ductor/named_sessions.json`

Storage is JSON + atomic write helpers (`atomic_json_save`).
I/O runs in worker threads (`asyncio.to_thread`).
