# cleanup/

Daily retention cleanup for workspace file-drop directories.

## Files

- `cleanup/observer.py`: `CleanupObserver`, retention execution, scheduler loop.
- `cleanup/__init__.py`: exports `CleanupObserver`.

## Purpose

Prevents unbounded growth of:

- `~/.ductor/workspace/telegram_files/`
- `~/.ductor/workspace/output_to_user/`

## Config (`AgentConfig.cleanup`)

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `true` | Master toggle |
| `telegram_files_days` | `int` | `30` | Retention for `telegram_files` |
| `output_to_user_days` | `int` | `30` | Retention for `output_to_user` |
| `check_hour` | `int` | `3` | Local hour (`user_timezone`) when cleanup is eligible |

## Lifecycle

`CleanupObserver.start()`:

1. exits early if `cleanup.enabled=false`
2. starts background loop with crash callback logging

Loop behavior:

1. wakes every hour
2. resolves local time via `resolve_user_timezone(config.user_timezone)`
3. runs cleanup only when `now.hour == check_hour`
4. runs at most once per day (`_last_run_date` guard)

## Deletion Rules

`_delete_old_files(directory, max_age_days)`:

- deletes files older than `max_age_days`
- top-level files only (non-recursive)
- ignores subdirectories
- logs warnings on per-file deletion errors

## Wiring

- created in `Orchestrator.__init__`
- started in `Orchestrator.create()`
- stopped in `Orchestrator.shutdown()`
