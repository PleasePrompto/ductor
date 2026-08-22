# Telegram Background Task Progress Design

Date: 2026-08-16
Executor: Codex

## Outcome

When Ductor successfully creates a TaskHub worker from a Telegram request, Telegram shows one authoritative status message immediately and keeps editing that same message until the final task result is delivered. The status remains alive while the parent model reviews the worker result, which is the silent phase observed in production.

## Evidence and problem boundary

The reported message corresponded to real TaskHub task 750eb552. The worker ran for 32.68 seconds, then parent-session review ran non-streaming for 185.44 seconds. TaskHub currently exposes completion and question callbacks only. MessageBus injects the result into the parent session before transport delivery, so Telegram receives nothing during parent review.

Raw model commentary is not a valid progress source. v0.20.2 intentionally suppresses model text adjacent to tool events to prevent thinking and internal tool narration from leaking.

## Considered approaches

1. Forward model commentary. Rejected because it reintroduces the leak fixed in v0.20.2 and cannot prove a worker exists.
2. Send a new Telegram message every interval. Rejected because concurrent long tasks would spam group topics.
3. Recommended: emit authoritative TaskHub lifecycle events and let Telegram maintain one editable status message with a local timer.

## Architecture

TaskHub emits TaskProgress events only after a registry entry exists:

- running: worker was created and is executing.
- reviewing: worker returned and the parent session is preparing the final response.

The existing TaskResult remains the terminal event. The new TaskProgress event uses the existing Envelope and MessageBus path with no session lock and no prompt injection.

TelegramTransport delegates status ownership to a small tracker keyed by chat_id, topic_id, and task_id. On running it sends a plain-text message and starts a 30-second asyncio heartbeat. Each tick edits the same message_id with elapsed time. On reviewing it changes the phase but keeps the timer alive. On TaskResult it first stops and awaits the timer, then edits the same message to done, failed, cancelled, or waiting before delivering the final response.

Matrix and Slack keep their existing terminal behavior. Their BotProtocol implementations accept TaskProgress as a no-op so the change remains Telegram-specific without breaking multi-transport stacks.

## Error handling and concurrency

- A failed initial send never fails the worker; a later progress event may retry.
- message is not modified is treated as success.
- Telegram rate limits sleep for retry_after and retry one edit.
- A deleted/uneditable status message is replaced once, and future ticks edit the replacement.
- Terminal handling marks the tracker final before cancelling its timer so a late tick cannot overwrite the terminal text.
- Resume of the same task_id starts a fresh tracker generation.
- Telegram shutdown cancels and awaits every tracker timer before closing the Bot session.
- Names are displayed as plain text; original prompts, tool parameters, upstream details and model reasoning are never included.

## Configuration

TasksConfig gains:

- progress_updates: true by default.
- progress_interval_seconds: 30.0, minimum 10 seconds.

Existing user configurations gain defaults through Ductor's deep-merge behavior.

## Testing

Tests must prove:

- no running event is emitted when submit fails;
- running precedes CLI execution and reviewing precedes result delivery;
- one task edits one message_id across repeated ticks;
- reviewing continues ticking while final result injection is blocked;
- terminal statuses stop the timer and cannot be overwritten;
- chat/topic/task keys isolate concurrent work;
- send, edit, rate-limit and shutdown failures do not affect task execution;
- multi-messenger wiring remains valid;
- config defaults, examples and docs agree.

## Non-goals

- Streaming the parent review.
- Displaying tool-level or token-level detail.
- Re-enabling arbitrary pre-tool model text.
- Changing payment-system data or task-worker permissions.
