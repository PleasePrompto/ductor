# bot/

Telegram interface layer (aiogram 3.x). Handles incoming updates, middleware, welcome/help UX, streaming output, callback routing, and rich file/text delivery.

## Files

- `app.py`: `TelegramBot` lifecycle, handler registration, startup/shutdown, restart watcher, webhook wake bridge.
- `handlers.py`: helper handlers for abort, orchestrator command routing, new session reset.
- `middleware.py`: `AuthMiddleware`, `SequentialMiddleware`, quick-command bypass, per-chat lock.
- `welcome.py`: `/start` welcome text + quick-start keyboard (`w:*` callbacks).
- `streaming.py`: append-mode stream editor + `create_stream_editor()` factory.
- `edit_streaming.py`: default in-place edit stream editor.
- `sender.py`: `send_rich()`, `send_file()`, `<file:...>` extraction, keyboard attachment.
- `formatting.py`: Markdown -> Telegram HTML conversion + chunk splitting.
- `buttons.py`: `[button:...]` parsing/stripping and inline keyboard generation.
- `media.py`: Telegram media download, `_index.yaml` rebuild, media prompt composition.
- `abort.py`: `/stop` and bare-word abort trigger detection.
- `dedup.py`: short-lived dedupe cache by `chat_id:message_id`.
- `typing.py`: typing-indicator context manager.

## Handler and Command Ownership

Registered in `TelegramBot._register_handlers()`:

- direct bot handlers: `/start`, `/help`, `/stop`, `/restart`, `/new`
- command route to orchestrator: `/status`, `/memory`, `/model`, `/cron`, `/diagnose`, `/upgrade`
- fallback message handler: all other messages
- callback query handler: all inline keyboard callbacks

`/restart` is bot-local (`app.py`), not an orchestrator command.

## Middleware Behavior

`AuthMiddleware`:

- drops updates from users outside `allowed_user_ids`.

`SequentialMiddleware` order (message updates only):

1. abort trigger check (`/stop` + bare abort words), handled before lock.
2. quick command check (`/status`, `/memory`, `/cron`, `/diagnose`), bypasses lock.
3. dedupe by `chat_id:message_id`.
4. per-chat `asyncio.Lock` for regular messages.

Callback queries are not processed through `SequentialMiddleware.__call__`; `TelegramBot` acquires the same lock explicitly for callback execution paths.

## Message Resolution

`TelegramBot._resolve_text()`:

- media messages -> `resolve_media_text()` (download + index + generated prompt text),
- plain text -> `strip_mention(...)` (removes `@botname` when present),
- non-text/non-media -> ignored.

For media in groups, processing only happens when addressed to the bot (reply or mention in caption).

## Streaming Flow

`TelegramBot._handle_streaming()`:

1. build stream editor (`EditStreamEditor` by default, `StreamEditor` in append mode).
2. create `StreamCoalescer` with config thresholds.
3. route deltas/tool events/system status from orchestrator callbacks into coalescer/editor.
4. system status callback: on `"compacting"` status, flush coalescer and show `[COMPACTING: Context full, conversation is compacting...]` via `editor.append_system()`.
5. on completion: flush + `editor.finalize(full_text)`.
6. output rules:
   - if stream fallback or no content streamed: send full text via `send_rich()`,
   - otherwise send only `<file:...>` tags via `send_files_from_text()`.

Both `EditStreamEditor` and `StreamEditor` support `append_system(text)` for rendering system indicators as italic HTML.

## Buttons

Syntax in model output:

```text
[button:Label]
```

Behavior (`buttons.py`):

- markers inside code blocks/inline code are ignored.
- same line => same keyboard row.
- callback data is UTF-8 truncated to 64 bytes.
- markers are stripped from visible text.

## Rich Sending and File Safety

`send_rich()`:

1. extract `<file:...>` tags,
2. strip file tags from text,
3. resolve keyboard (`reply_markup` override or `[button:...]` extraction),
4. send text chunks,
5. attach keyboard to last text message,
6. send referenced files.

`send_file()`:

- checks `allowed_roots` (via `is_path_safe`) before sending.
- if blocked: sends a user-visible block message.
- image-like files -> `send_photo`, others -> `send_document`.

Bot currently passes `allowed_roots=[paths.workspace]`.

## Callback Queries

`TelegramBot._on_callback_query()`:

- always calls `answer()`.
- `w:*` callbacks -> resolved via `welcome.py` into full prompt text.
- `upg:*` callbacks -> upgrade flow (`upg:yes:<version>` triggers upgrade + restart, `upg:no` dismisses).
- `ms:*` callbacks -> model selector wizard (edits message in place).
- all other callbacks:
  - remove original keyboard,
  - run callback text through normal message pipeline under per-chat lock.

## Webhook Wake Bridge

`TelegramBot._handle_webhook_wake(chat_id, prompt)`:

1. acquires `SequentialMiddleware.get_lock(chat_id)`.
2. calls `Orchestrator.handle_message(chat_id, prompt)`.
3. sends result via `send_rich()`.

`_on_webhook_result()` only forwards `cron_task` webhook results. `wake` responses are already sent by `_handle_webhook_wake()`.

## Heartbeat Delivery

`_on_heartbeat_result(chat_id, text)` receives non-ACK heartbeat alerts and delivers them via `send_rich()`. Logs at `DEBUG` on entry and `INFO` on successful delivery for end-to-end observability.

## Update System Integration

- `UpdateObserver` starts in `_on_startup()` only for upgradeable installs (`pipx`/`pip`, not dev/source), and stops in `shutdown()`.
- On new version detected: `_on_update_available(info)` sends notification with inline buttons to all `allowed_user_ids`.
- `_handle_upgrade_callback(chat_id, message_id, data)` handles `upg:yes:<version>` (run upgrade, write sentinel, exit 42) and `upg:no` (dismiss).
- On startup: `consume_upgrade_sentinel()` reads and deletes sentinel, sends "Upgrade complete" message.

## Restart Behavior in Bot

- `/restart`: write restart sentinel + set exit code `42` + stop polling.
- `/upgrade` (via callback): write upgrade sentinel + set exit code `42` + stop polling.
- background watcher polls `restart-requested` every 2s and triggers same restart path.
- startup consumes restart sentinel and upgrade sentinel, sends confirmations to recorded chat.
