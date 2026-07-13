# config_reload.py

Centralized runtime config hot-reload watcher.

## File

- `ductor_bot/config_reload.py`: `ConfigReloader`, config diff/classification helpers

## Purpose

Avoid unnecessary restarts when safe `config.json` fields change.

`ConfigReloader` polls `~/.ductor/config/config.json` every 5 seconds, compares the file content (so equal-timestamp and equal-size rewrites are not missed), validates with `AgentConfig`, diffs top-level schema fields, and:

- applies hot-reloadable fields immediately,
- logs restart-required field changes through callback.

## Public pieces

- `diff_configs(old, new) -> dict[field, (old, new)]`
- `classify_changes(changes) -> (hot_values, restart_fields)`
- `ConfigReloader.start() / stop()`

## Hot-reloadable fields

- `model`, `provider`, `reasoning_effort`
- `cli_timeout`, `max_budget_usd`, `max_turns`, `max_session_messages`
- `idle_timeout_minutes`, `session_age_warning_hours`, `daily_reset_hour`, `daily_reset_enabled`
- `permission_mode`, `file_access`, `user_timezone`
- `streaming`, `heartbeat`, `cleanup`, `cli_parameters`
- `allowed_user_ids`, `allowed_group_ids`, `group_mention_only`

Important runtime nuance:

- `heartbeat` and `cleanup` values are hot-applied to config objects, but observer lifecycle is not toggled on reload.
- if heartbeat/cleanup were disabled at startup, switching `enabled=true` requires restart.
- when already running, updated values are picked up on subsequent loop cycles.

## Restart-required fields

- transport/auth: `transport`, `transports`, `telegram_token`, `matrix`
- runtime topology: `docker`, `api`, `webhooks`, `interagent_port`
- environment/core: `ductor_home`, `log_level`, `gemini_api_key`, `update_check`
- timeout/task policy: `timeouts`, `tasks`
- classification is schema-based over `AgentConfig` top-level fields: any changed top-level field not in the hot-reloadable set is reported as restart-required.

Timeout note:

- `timeouts.*` updates currently require restart.
- `tasks.*` updates currently require restart.
- runtime hot-reload still applies `cli_timeout` changes immediately.

## Wiring

`Orchestrator.create()` creates and starts the reloader with:

- `on_hot_reload` -> `Orchestrator._on_config_hot_reload(...)`
- `on_restart_needed` -> warning logger callback

`Orchestrator.shutdown()` stops it via `orchestrator/lifecycle.shutdown(...)` -> `ObserverManager.stop_all()`.
