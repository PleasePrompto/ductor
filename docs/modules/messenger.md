# messenger/

Transport abstraction layer: protocols, capabilities, registry, and
multi-transport adapter. Everything in this package is
transport-agnostic. Concrete transports live in sub-packages
(`messenger/telegram/`, `messenger/matrix/`, `messenger/slack/`).

For transport-specific details see [bot.md](bot.md) (Telegram) and
[matrix.md](matrix.md) (Matrix).

## Files

| File | Purpose |
|---|---|
| `messenger/__init__.py` | Public re-exports for protocols, command classification, send options, multi-transport helpers, and bot factory |
| `messenger/commands.py` | Shared direct/orchestrator/multi-agent command sets + `classify_command()` |
| `messenger/callback_router.py` | Shared callback-data dispatch helpers for selector/button routing |
| `messenger/protocol.py` | `BotProtocol` — runtime-checkable interface every transport implements |
| `messenger/capabilities.py` | `MessengerCapabilities` dataclass + per-transport presets |
| `messenger/registry.py` | `create_bot()` factory + `_TRANSPORT_FACTORIES` dispatch table |
| `messenger/notifications.py` | `NotificationService` protocol + `CompositeNotificationService` fan-out |
| `messenger/send_opts.py` | Base send-option model shared by transport senders |
| `messenger/multi.py` | `MultiBotAdapter` — multi-transport facade behind `BotProtocol` |

## BotProtocol

`BotProtocol` (`protocol.py`) is a `typing.Protocol` decorated with
`@runtime_checkable`. The supervisor, `AgentStack`, and
`InterAgentBus` depend **only** on this protocol, never on
transport-specific classes.

Required surface:

| Member | Kind | Description |
|---|---|---|
| `orchestrator` | property | Current `Orchestrator` (or `None` before startup) |
| `config` | property | `AgentConfig` |
| `notification_service` | property | `NotificationService` |
| `run()` | async | Start event loop, block until shutdown, return exit code |
| `shutdown()` | async | Graceful teardown |
| `register_startup_hook(hook)` | method | Callback invoked after orchestrator creation |
| `set_abort_all_callback(cb)` | method | Multi-agent abort injection point |
| `on_async_interagent_result(result)` | async | Deliver async inter-agent result |
| `on_task_result(result)` | async | Deliver background task completion |
| `on_task_question(...)` | async | Deliver background task question |
| `file_roots(paths)` | method | Allowed root directories for file sends |

`TelegramBot`, `MatrixBot`, and `SlackBot` implement this protocol.

## MessengerCapabilities

`MessengerCapabilities` (`capabilities.py`) is a frozen, slotted
dataclass that declares what a transport supports:

| Field | Type | Default |
|---|---|---|
| `name` | `str` | `""` |
| `supports_inline_buttons` | `bool` | `False` |
| `supports_reactions` | `bool` | `False` |
| `supports_message_editing` | `bool` | `False` |
| `supports_threads` | `bool` | `False` |
| `supports_typing_indicator` | `bool` | `True` |
| `supports_file_send` | `bool` | `True` |
| `supports_streaming_edit` | `bool` | `False` |
| `supports_seen_indicator` | `bool` | `False` |
| `max_message_length` | `int` | `4096` |

Two presets are shipped:

| Preset | Key differences |
|---|---|
| `TELEGRAM_CAPABILITIES` | inline buttons, message editing, threads, streaming edit, seen indicator, 4096 char limit |
| `MATRIX_CAPABILITIES` | reactions (no inline buttons), no message editing, no threads, seen indicator, 40000 char limit |
| `SLACK_CAPABILITIES` | threads, message editing, file send, 40000 char limit |

Orchestrator and delivery code queries capabilities at runtime to
decide between streaming-edit vs. segment-based streaming, inline
buttons vs. reaction buttons, etc.

`supports_seen_indicator` signals whether the transport can acknowledge incoming messages with a "seen" indicator. Telegram uses an emoji reaction; Matrix uses a read receipt. The feature is gated by `config.scene.seen_reaction` -- even when the capability is `True`, seen indicators are only sent when enabled in config.

Telegram's newer stage-based `scene.status_reaction` behavior is intentionally transport-specific and currently lives in `messenger/telegram/message_dispatch.py`; it is not modeled through `supports_reactions`.

## Transport Registry

`create_bot()` (`registry.py`) is the single entry point for bot
construction. It inspects `config.is_multi_transport`:

- **Single transport**: looks up the transport name in
  `_TRANSPORT_FACTORIES` and calls the matching factory.
- **Multi transport**: returns a `MultiBotAdapter` wrapping all
  configured transports.

`_TRANSPORT_FACTORIES` is a `dict[str, _Factory]` mapping transport
names to lazy-import factory functions:

```python
_TRANSPORT_FACTORIES: dict[str, _Factory] = {
    "telegram": _create_telegram,
    "matrix": _create_matrix,
    "slack": _create_slack,
}
```

Each factory accepts `(config, *, agent_name, bus, lock_pool)` and
returns a `BotProtocol`. Imports are deferred inside the factory body
so that unused transports do not need their dependencies installed.

Raises `ValueError` for unknown transport names.

## NotificationService

`NotificationService` (`notifications.py`) is a runtime-checkable
protocol with two methods:

- `notify(chat_id, text)` — send to a specific chat/room.
- `notify_all(text)` — broadcast to all authorized users/rooms.

Both `TelegramNotificationService` and `MatrixNotificationService`
implement this protocol. The supervisor and bus use it without
knowing which transport is active.

Transport-specific startup/update routing sits above this protocol:

- Telegram routes startup notices through `notifications.startup_targets` and upgrade notices through `notifications.upgrade_targets` when those lists contain enabled targets
- Matrix currently routes startup/restart/update notices through `notify_startup()` and `notifications.startup_targets`; there is no separate Matrix-only upgrade-target path today

### CompositeNotificationService

`CompositeNotificationService` fans out calls to multiple underlying
services. It holds a `list[NotificationService]` and iterates
sequentially on both `notify()` and `notify_all()`.

Used by `MultiBotAdapter` to aggregate all transports' notification
services.

## Multi-Transport Mode

`MultiBotAdapter` (`multi.py`) wraps multiple transport bots behind
a single `BotProtocol` facade. It is returned by `create_bot()` when
`config.is_multi_transport` is true.

### Construction

1. Creates a shared `LockPool` and `MessageBus`.
2. Iterates `config.transports` and calls `_create_single_bot()` for
   each, injecting the shared bus and lock pool.
3. First bot becomes the **primary**; the rest are **secondaries**.
4. Builds a `CompositeNotificationService` from all bots.

### Startup sequence (`run()`)

1. Registers a startup hook on the primary that sets an
   `asyncio.Event`.
2. Launches the primary bot as an `asyncio.Task`.
3. Waits for the orchestrator-ready event.
4. Injects the primary's orchestrator into all secondary bots.
5. Launches secondary bots as tasks.
6. `asyncio.wait(FIRST_COMPLETED)` — when any bot finishes, the
   rest are cancelled.
7. Returns the exit code from the first completed bot (e.g. `42`
   for restart).

### Delegation rules

| Method | Delegation |
|---|---|
| `orchestrator` | primary |
| `config` | own `_config` |
| `notification_service` | `CompositeNotificationService` |
| `register_startup_hook` | primary |
| `set_abort_all_callback` | all bots |
| `on_async_interagent_result` | all bots |
| `on_task_result` | all bots |
| `on_task_question` | all bots |
| `file_roots` | primary |
| `shutdown` | all bots |

### Shared resources

All bots in a `MultiBotAdapter` share:

- `MessageBus` — single instance for cross-transport envelope delivery
- `LockPool` — single instance for per-chat locking
- `Orchestrator` — created by the primary, injected into secondaries

## Adding a New Transport

1. **Create the sub-package** `messenger/<name>/` with at least a bot
   module implementing `BotProtocol`.

2. **Define capabilities** in `capabilities.py`:

   ```python
   DISCORD_CAPABILITIES = MessengerCapabilities(
       name="discord",
       supports_inline_buttons=True,
       # ...
   )
   ```

3. **Add a factory** in `registry.py`:

   ```python
   def _create_discord(
       config: AgentConfig,
       *,
       agent_name: str,
       bus: MessageBus | None,
       lock_pool: LockPool | None,
   ) -> BotProtocol:
       from ductor_bot.messenger.discord.bot import DiscordBot
       return DiscordBot(config, agent_name=agent_name,
                         bus=bus, lock_pool=lock_pool)

   _TRANSPORT_FACTORIES["discord"] = _create_discord
   ```

4. **Implement `NotificationService`** for the transport so
   `CompositeNotificationService` can include it.

5. **Add a `MessageBus` transport adapter** (`transport.py`) that maps
   `Envelope` objects to the transport's native send API.

6. **Guard the dependency** behind an optional extra in
   `pyproject.toml` and use deferred imports in the factory so the
   package is not required unless the transport is selected.

7. **Add config fields** to `AgentConfig` for the new transport
   (credentials, allowed users/rooms, etc.).

8. **Write tests** — mock the transport client and verify the bot
   satisfies `isinstance(bot, BotProtocol)`.
