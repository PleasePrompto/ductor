# logging (cross-cutting)

Context-aware logging infrastructure used by all runtime modules.

## Files

- `ductor_bot/log_context.py`: `ContextVar` state (`operation`, `chat_id`, `session_id`) + `ContextFilter`.
- `ductor_bot/logging_config.py`: root logger setup (console + rotating file via queue listener).

## Context Model

`ContextFilter` injects `record.ctx` into every log line as:

```text
[operation:chat_id:session_id_8]
```

Missing values are omitted.

Operation codes currently used:

- `msg`: incoming Telegram message (`SequentialMiddleware`)
- `cb`: callback query (`TelegramBot._on_callback_query`)
- `cron`: cron execution (`CronObserver._execute_job`)
- `hb`: heartbeat run (`HeartbeatObserver._run_for_chat`)
- `wh`: webhook request / webhook wake dispatch

`set_log_context()` updates the context for the current async task; child tasks inherit current context.

## Output Sinks

`setup_logging()` configures:

- colored console logs (`stderr`)
- rotating file logs in `~/.ductor/logs/agent.log` (`5MB`, `3` backups)

File logging uses `QueueHandler` + `QueueListener` so file I/O does not block the event loop.

## Runtime Control

- default level is `INFO`
- `--verbose` forces `DEBUG`
- `config.log_level` is applied at startup when `--verbose` is not set

## Conventions in Codebase

- loggers are module-local: `logger = logging.getLogger(__name__)`
- log calls use lazy `%` formatting (not f-strings)
- exceptions use `logger.exception(...)` in `except` blocks
