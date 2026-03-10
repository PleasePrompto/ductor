# bot/

Telegram interface layer (`aiogram`): handlers, middleware, callback routing, streaming UX, startup lifecycle.

For the Matrix transport equivalent, see [matrix.md](matrix.md).

## Files

- `bot/app.py`: `TelegramBot` class, handler registration, callback routing, group management commands
- `bot/startup.py`: startup sequence (orchestrator creation, bus wiring, recovery, sentinels)
- `bot/callbacks.py`: shared selector callback helpers (`SelectorResponse` editing)
- `bot/middleware.py`: `AuthMiddleware`, `SequentialMiddleware`, queue controls, quick-command bypass
- `bot/message_dispatch.py`: shared streaming/non-streaming execution paths
- `bot/handlers.py`: command helper handlers (`/new`, `/stop`, `/interrupt`, generic command path)
- `bot/chat_tracker.py`: persisted group chat activity (`chat_activity.json`) for `/where` and group audits
- `bot/topic.py`: topic/session key helpers + topic-name cache
- `bot/file_browser.py`, `bot/sender.py`, `bot/media.py`, `bot/welcome.py`, `bot/formatting.py`, `bot/typing.py`

## Command ownership

Bot-level handlers:

- `/start`, `/help`, `/info`, `/showfiles`, `/stop`, `/interrupt`, `/stop_all`, `/restart`, `/new`, `/session`, `/sessions`, `/tasks`, `/agent_commands`
- main-agent only: `/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`
- hidden but supported: `/where`, `/leave` (not in Telegram command popup)

Orchestrator-routed commands:

- `/status`, `/memory`, `/model`, `/cron`, `/diagnose`, `/upgrade`, `/sessions`, `/tasks`

## Middleware behavior

### `AuthMiddleware`

- private chats: requires `user_id in allowed_user_ids`
- groups/supergroups: requires both
  - `group_id in allowed_group_ids`
  - `user_id in allowed_user_ids`
- optional rejected-group callback feeds `ChatTracker`

`group_mention_only` is not auth. It is applied later as message-content gating.

### `SequentialMiddleware`

Flow order:

1. interrupt/abort checks before lock (`/interrupt`, `/stop_all`, `/stop`, abort phrases)
2. quick-command bypass
3. dedupe by `chat_id:message_id`
4. queue indicator when lock is busy (`mq:<entry_id>` cancel callback)
5. lock by `SessionKey.lock_key` (topic-aware)

Quick commands:

- `/status`, `/memory`, `/cron`, `/diagnose`, `/model`, `/showfiles`, `/sessions`, `/tasks`, `/where`, `/leave`

Queue APIs:

- `is_busy(chat_id)`
- `has_pending(chat_id)`
- `cancel_entry(chat_id, entry_id)`
- `drain_pending(chat_id)`

## Topic support

`get_session_key(message)` returns:

- `SessionKey(chat_id, topic_id=message_thread_id)` for forum topic messages
- `SessionKey(chat_id, topic_id=None)` otherwise

Implications:

- forum topics are fully isolated sessions
- locks are topic-aware
- `/new` and `/model` operate per topic when called inside a topic
- `/new @topicname` resets a specific topic session by cached name lookup

`TopicNameCache` is seeded from persisted sessions at startup and updated from forum topic create/edit events.

## Group management (`/where`, `/leave`, audits)

`ChatTracker` stores activity in `~/.ductor/chat_activity.json`.

- join/reject/leave events are persisted
- `/where` shows active/rejected/left groups
- `/leave <group_id>` lets an authorized user force leave
- startup and periodic (24h) group audits auto-leave groups no longer in `allowed_group_ids`
- auth hot-reload (`allowed_group_ids`) triggers immediate audit task

## Message dispatch (`message_dispatch.py`)

### Non-streaming

`run_non_streaming_message()`:

- typing context
- `orchestrator.handle_message()`
- `send_rich()`

### Streaming

`run_streaming_message()`:

- stream editor + `StreamCoalescer`
- forward text/tool/system callbacks
- finalize editor
- fallback rules:
  - stream fallback or empty stream -> `send_rich(full_text)`
  - otherwise send only extracted files from final text

## Callback routing

Special callback namespaces:

- `mq:*` queue cancel
- `upg:*` upgrade flow
- `ms:*` model selector
- `crn:*` cron selector
- `nsc:*` session selector
- `tsc:*` task selector
- `ns:*` named-session follow-up callbacks
- `sf:*` / `sf!` file browser

Selector callbacks use shared helpers in `bot/callbacks.py` and selector response types from `orchestrator/selectors/models.py`.

## Observer and task integration

The bot no longer owns fragmented `deliver_*` handlers.

Current model:

- startup wires observer outputs to `MessageBus` via `orch.wire_observers_to_bus(...)`
- async inter-agent and task callbacks convert results through `bus/adapters.py`
- `MessageBus` handles lock/injection/delivery using shared `LockPool`

Webhook wake path:

- acquires lock for target chat
- runs orchestrator message flow
- submits final wake result envelope for delivery

## Startup lifecycle (`bot/startup.py`)

Startup performs, in order:

1. orchestrator creation
2. topic cache seeding and resolver wiring
3. restart/upgrade sentinel handling
4. observer-to-bus wiring
5. startup-kind detection and startup notification policy
6. recovery planner actions
7. command sync + restart marker watcher
8. group audit startup + periodic audit loop

## File safety

Outbound file sends enforce `file_access` via `files.allowed_roots.resolve_allowed_roots(...)`.
`sender.py` uses shared MIME/tag helpers from `files/`.
