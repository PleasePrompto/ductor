# messenger/telegram/

Telegram interface layer (`aiogram`): handlers, middleware, callback routing, streaming UX, startup lifecycle.

For the Matrix transport equivalent, see [matrix.md](matrix.md).
For shared messenger protocols and the transport registry, see
[messenger.md](messenger.md).

## Files

- `messenger/telegram/app.py`: `TelegramBot` class, handler registration, callback routing, group management commands
- `messenger/telegram/startup.py`: startup sequence (orchestrator creation, bus wiring, recovery, sentinels)
- `messenger/telegram/callbacks.py`: shared selector callback helpers (`SelectorResponse` editing)
- `messenger/telegram/middleware.py`: `AuthMiddleware`, `SequentialMiddleware`, queue controls, quick-command bypass
- `messenger/telegram/message_dispatch.py`: shared streaming/non-streaming execution paths
- `messenger/telegram/handlers.py`: command helper handlers (`/new`, `/stop`, generic command path)
- `messenger/telegram/chat_tracker.py`: persisted group chat activity (`chat_activity.json`) for `/where` and group audits
- `messenger/telegram/topic.py`: topic/session key helpers + topic-name cache
- `messenger/telegram/file_browser.py`, `messenger/telegram/sender.py`, `messenger/telegram/media.py`, `messenger/telegram/welcome.py`, `messenger/telegram/formatting.py`, `messenger/telegram/typing.py`
- `messenger/telegram/transport.py`: Telegram transport adapter for `MessageBus`

## Command ownership

Bot-level handlers:

- `/start`, `/help`, `/info`, `/files`, `/stop`, `/stop_all`, `/restart`, `/new`, `/reset`, `/session`, `/sessions`, `/tasks`, `/agent_commands`
- main-agent only: `/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`
- hidden but supported: `/where`, `/leave` (not in Telegram command popup)

Immediate middleware-handled command path (pre-lock, no normal dispatch):

- `/interrupt` (and bare-word interrupt triggers like `esc`, `interrupt`)

Orchestrator-routed commands:

- `/status`, `/memory`, `/model`, `/cron`, `/diagnose`, `/upgrade`, `/sessions`, `/tasks`, `/reset`

## Middleware behavior

### `AuthMiddleware`

- private chats: requires `user_id in allowed_user_ids`
- groups/supergroups: requires both
  - `group_id in allowed_group_ids`
  - `user_id in allowed_user_ids`
- optional rejected-group callback feeds `ChatTracker`

`group_mention_only` is not auth. It is applied later as message-content gating.

Media messages in groups also respect `group_mention_only`:

- when `group_mention_only=false`: media is always processed in authorized groups (no mention required)
- when `group_mention_only=true`: media requires a mention or reply addressing the bot (same gate as text messages)

This is checked in `_resolve_text()` via `is_media_addressed(message, bot_id, bot_username)`.

### `SequentialMiddleware`

Flow order:

1. interrupt/abort checks before lock (`/interrupt`, `/stop_all`, `/stop`, abort phrases)
2. quick-command bypass
3. dedupe by `chat_id:message_id`
4. queue indicator when lock is busy (`mq:<entry_id>` cancel callback)
5. lock by `SessionKey.lock_key` (topic-aware)

Quick commands:

- `/status`, `/memory`, `/cron`, `/diagnose`, `/model`, `/files`, `/sessions`, `/tasks`, `/where`, `/leave`

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

- join/reject/leave events are persisted for both groups and channels
- `/where` shows active/rejected/left tracked chats, including channels
- `/leave <group_id>` lets an authorized user force leave
- startup and periodic (24h) audits auto-leave groups no longer in `allowed_group_ids` and channels no longer in `allowed_channel_ids`
- auth hot-reload (`allowed_group_ids`) triggers immediate audit task
- `allowed_channel_ids` is supported by the bot runtime and audit path, but config hot-reload does not currently emit that field, so channel allowlist changes still need restart in practice

## Reply context (`build_reply_prompt`, `prepend_reply_to_media`)

When a user uses Telegram's native reply gesture, `_resolve_text()` prepends the
cited message to the prompt. Both helpers share `_cited_reply_text()`, which
prefers the user-selected quote fragment (Bot API 7.0+, `message.quote`) over the
full replied-to body (`reply_to_message.text` or `.caption`).

Text replies (`build_reply_prompt`) wrap both parts with explicit labels:

```text
The user is replying to this quoted message:
> <quoted text>

The user's message:
<user input>
```

Media replies (`prepend_reply_to_media`) — where the reply is a voice / photo /
video / ... attachment instead of text — prepend the quote plus an attachment-type
note ahead of the `[INCOMING FILE]` block, so the agent knows the voicemail/image
is the reply to the quote (and can transcribe it with that context):

```text
The user is replying to this quoted message:
> <quoted text>

Their reply is a voice message (the attached file below).

[INCOMING FILE]
...
```

Both return the body unchanged when the message is not a reply or the cited
message has no text (forum-topic service messages).

## Message dispatch (`message_dispatch.py`)

### Non-streaming

`run_non_streaming_message()`:

- typing context
- `orchestrator.handle_message()`
- `send_rich()`

### Streaming

`run_streaming_message()`:

- stream editor + `StreamCoalescer`
- optional `ReactionTracker` on the original Telegram message when `scene.status_reaction=true`
- forward text/tool/system callbacks
- finalize editor
- fallback rules:
  - stream fallback or empty stream -> `send_rich(full_text)`
  - otherwise send only extracted files from final text

Reaction behavior:

- `ReactionTracker` maps thinking/system/tool stages to Telegram-safe emoji and dedups consecutive identical states
- reactions are best-effort and are always cleared at the end of the turn
- when `scene.status_reaction=false` and `scene.seen_reaction=true`, the app falls back to the older one-shot seen reaction instead

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

Selector callbacks use shared helpers in `messenger/telegram/callbacks.py` and selector response types from `orchestrator/selectors/models.py`.

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

Current limitation:

- Telegram startup wires the webhook wake handler
- Matrix startup currently does not, so webhook `wake` is effectively Telegram-only right now

## Startup lifecycle (`messenger/telegram/startup.py`)

Startup performs, in order:

1. orchestrator creation
2. topic cache seeding and resolver wiring
3. restart/upgrade sentinel handling
4. observer-to-bus wiring
5. startup-kind detection and startup notification policy
6. recovery planner actions
7. command sync + restart marker watcher
8. group audit startup + periodic audit loop

Notification routing during startup is transport-owned:

- startup/restart notices use `notifications.startup_targets` when configured, otherwise they broadcast to allowed users
- upgrade-available notices use `notifications.upgrade_targets` when configured, otherwise they broadcast normally

## File safety

Outbound file sends enforce `file_access` via `files.allowed_roots.resolve_allowed_roots(...)`.
`messenger/telegram/sender.py` uses shared MIME/tag helpers from `files/`.
