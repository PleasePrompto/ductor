# bot/

Telegram interface layer (aiogram 3.x). Handles incoming updates, middleware, welcome/help UX, streaming output, callback routing, and rich file/text delivery.

## Files

- `app.py`: `TelegramBot` lifecycle, handler registration, startup/shutdown, restart watcher, webhook wake bridge.
- `handlers.py`: helper handlers for abort, orchestrator command routing, new session reset.
- `middleware.py`: `AuthMiddleware`, `SequentialMiddleware`, quick-command bypass, per-chat lock, queue entry tracking with cancel buttons.
- `welcome.py`: `/start` welcome text + quick-start keyboard (`w:*` callbacks).
- `file_browser.py`: interactive `~/.ductor/` navigator via inline keyboard (`sf:` / `sf!` callbacks).
- `response_format.py`: shared formatting primitives (`SEP`, `fmt()`, `NEW_SESSION_TEXT`, `stop_text()`).
- `streaming.py`: append-mode stream editor + `create_stream_editor()` factory.
- `edit_streaming.py`: default in-place edit stream editor.
- `sender.py`: `send_rich()`, `send_file()`, `<file:...>` extraction, keyboard attachment.
- `formatting.py`: Markdown -> Telegram HTML conversion (bold, italic, code, tables, blockquotes) + chunk splitting.
- `buttons.py`: `[button:...]` parsing/stripping and inline keyboard generation.
- `media.py`: Telegram media download, `_index.yaml` rebuild, media prompt composition.
- `abort.py`: `/stop` and bare-word abort trigger detection.
- `dedup.py`: short-lived dedupe cache by `chat_id:message_id`.
- `typing.py`: typing-indicator context manager.

## Handler and Command Ownership

Registered in `TelegramBot._register_handlers()`:

- direct bot handlers: `/start`, `/help`, `/info`, `/showfiles`, `/stop`, `/restart`, `/new`
- command route to orchestrator: `/status`, `/memory`, `/model`, `/cron`, `/diagnose`, `/upgrade`
- fallback message handler: all other messages
- callback query handler: all inline keyboard callbacks

`/restart` is bot-local (`app.py`), not an orchestrator command.

## Middleware Behavior

`AuthMiddleware`:

- drops updates from users outside `allowed_user_ids`.

`SequentialMiddleware` order (message updates only):

1. abort trigger check (`/stop` + bare abort words), handled before lock. On abort: kills processes **and** drains the pending message queue (edits all indicators to `[Message discarded.]`).
2. quick command check (`/status`, `/memory`, `/cron`, `/diagnose`, `/model`, `/showfiles`), bypasses lock. `/showfiles` is handled directly (no orchestrator). `/model` has a busy-check: when agent is active or messages are queued, returns immediate feedback instead of the wizard.
3. dedupe by `chat_id:message_id`.
4. per-chat `asyncio.Lock` for regular messages. When the lock is held, queued messages get a `[Message in queue...]` indicator (reply to the user's message) with a "Cancel message" inline button (`mq:<entry_id>` callback).
5. after lock acquisition: cancelled entries skip handler execution; otherwise the indicator is deleted and the handler runs normally.

Queue management methods:

- `is_busy(chat_id)`: True if lock is held or pending entries exist.
- `has_pending(chat_id)`: True if pending entries exist.
- `cancel_entry(chat_id, entry_id)`: cancel a single queued message, edit indicator to `[Message cancelled.]`.
- `drain_pending(chat_id)`: cancel all pending messages, edit indicators to `[Message discarded.]`.

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
4. system status callback:
   - on `"thinking"` status, flush coalescer and show `[THINKING]`
   - on `"compacting"` status, flush coalescer and show `[COMPACTING]`
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

Bot passes roots from `config.file_access`:

- `"all"` -> no root restriction (`None`)
- `"home"` -> `[Path.home()]`
- `"workspace"` -> `[paths.workspace]`

## Callback Queries

`TelegramBot._on_callback_query()`:

- always calls `answer()`.
- `w:*` callbacks -> resolved via `welcome.py` into full prompt text.
- `mq:*` callbacks -> queue cancel: parses entry ID and calls `SequentialMiddleware.cancel_entry()`.
- `upg:*` callbacks -> upgrade flow:
  - `upg:cl:<version>` -> fetch/send changelog
  - `upg:yes:<version>` -> upgrade + restart
  - `upg:no` -> dismiss
- `ms:*` callbacks -> model selector wizard (edits message in place).
- `sf:*` / `sf!*` callbacks -> file browser: `sf:<rel_path>` navigates directories (edit message in place), `sf!<rel_path>` sends file-request prompt to orchestrator.
- all other callbacks:
  - append `[USER ANSWER] <label>` to original message when possible (fallback: keyboard-only removal),
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
- `_handle_upgrade_callback(chat_id, message_id, data)` handles:
  - `upg:cl:<version>` (fetch changelog),
  - `upg:yes:<version>` (run upgrade, write sentinel, exit 42),
  - `upg:no` (dismiss).
- On startup: `consume_upgrade_sentinel()` reads and deletes sentinel, sends "Upgrade complete" message.

## Restart Behavior in Bot

- `/restart`: write restart sentinel + set exit code `42` + stop polling.
- `/upgrade` (via callback): write upgrade sentinel + set exit code `42` + stop polling.
- background watcher polls `restart-requested` every 2s and triggers same restart path.
- startup consumes restart sentinel and upgrade sentinel, sends confirmations to recorded chat.
