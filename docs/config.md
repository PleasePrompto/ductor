# Configuration

Runtime config file: `~/.ductor/config/config.json`.

Seed source: `<repo>/config.example.json` (source checkout) or packaged fallback `ductor_bot/_config_example.json` (installed mode).

## Config Creation

Primary path: `ductor onboarding` (interactive wizard) writes `config.json` with user-provided values merged into `AgentConfig` defaults. See `docs/modules/setup_wizard.md`.

## Load & Merge Behavior

Config is merged in two places:

1. `ductor_bot/__main__.py::load_config()`
   - creates config on first start (copy from `config.example.json` or Pydantic defaults),
   - deep-merges runtime file with `AgentConfig` defaults,
   - writes back only when new keys were added.
2. `ductor_bot/workspace/init.py::_smart_merge_config()`
   - shallow merge `{**defaults, **existing}` with `config.example.json`,
   - preserves existing user top-level keys,
   - only fills missing top-level keys from `config.example.json`.

Runtime edits from `/model` and webhook token auto-generation are persisted via `update_config_file_async()`.

## `AgentConfig` (`ductor_bot/config.py`)

| Field | Type | Default | Notes |
|---|---|---|---|
| `log_level` | `str` | `"INFO"` | Applied at startup unless CLI `--verbose` is used |
| `provider` | `str` | `"claude"` | Default provider stored in session/config state |
| `model` | `str` | `"opus"` | Default model ID |
| `ductor_home` | `str` | `"~/.ductor"` | Runtime home root |
| `idle_timeout_minutes` | `int` | `1440` | Session freshness timeout (`0` = never expires, only `/new` resets) |
| `session_age_warning_hours` | `int` | `12` | Show `/new` reminder every 10th message after this age (`0` = disabled) |
| `daily_reset_hour` | `int` | `4` | Session daily reset boundary (in `user_timezone`) |
| `user_timezone` | `str` | `""` | IANA timezone (e.g. `"Europe/Berlin"`). Affects cron scheduling, daily session reset, and heartbeat quiet hours. Fallback: host system TZ, then UTC. |
| `max_budget_usd` | `float \| None` | `None` | Passed to Claude CLI |
| `max_turns` | `int \| None` | `None` | Passed to Claude CLI |
| `max_session_messages` | `int \| None` | `None` | Session rollover limit |
| `permission_mode` | `str` | `"bypassPermissions"` | Provider sandbox/approval behavior |
| `cli_timeout` | `float` | `600.0` | Timeout per CLI call (seconds) |
| `reasoning_effort` | `str` | `"medium"` | Codex reasoning level |
| `file_access` | `str` | `"all"` | File send restriction: `"all"` (no limit), `"home"` (user home dir), `"workspace"` (ductor workspace only) |
| `telegram_token` | `str` | `""` | Telegram bot token |
| `allowed_user_ids` | `list[int]` | `[]` | Telegram allowlist |
| `streaming` | `StreamingConfig` | see below | Streaming tuning |
| `docker` | `DockerConfig` | see below | Docker sidecar config |
| `heartbeat` | `HeartbeatConfig` | see below | Background heartbeat config |
| `webhooks` | `WebhookConfig` | see below | Webhook HTTP server config |

## `StreamingConfig`

| Field | Type | Default |
|---|---|---|
| `enabled` | `bool` | `true` |
| `min_chars` | `int` | `200` |
| `max_chars` | `int` | `4000` |
| `idle_ms` | `int` | `800` |
| `edit_interval_seconds` | `float` | `2.0` |
| `max_edit_failures` | `int` | `3` |
| `append_mode` | `bool` | `false` |
| `sentence_break` | `bool` | `true` |

## `DockerConfig`

| Field | Type | Default |
|---|---|---|
| `enabled` | `bool` | `false` |
| `image_name` | `str` | `"ductor-sandbox"` |
| `container_name` | `str` | `"ductor-sandbox"` |
| `auto_build` | `bool` | `true` |

`DockerManager` exists, but default runtime startup does not automatically call `DockerManager.setup()`. CLI calls run on host unless `docker_container` is explicitly wired.

## `HeartbeatConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `interval_minutes` | `int` | `30` | Loop interval |
| `cooldown_minutes` | `int` | `5` | Skip if user active recently |
| `quiet_start` | `int` | `21` | Quiet start hour (in `user_timezone`, inclusive) |
| `quiet_end` | `int` | `8` | Quiet end hour (in `user_timezone`, exclusive) |
| `prompt` | `str` | default prompt | Sent as heartbeat message |
| `ack_token` | `str` | `"HEARTBEAT_OK"` | Suppression token |

## `WebhookConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `host` | `str` | `"127.0.0.1"` | Bind address (localhost only by default) |
| `port` | `int` | `8742` | HTTP server port |
| `token` | `str` | `""` | Global bearer fallback token (auto-generated and persisted when webhooks start). Per-hook auth details live in `webhooks.json`. |
| `max_body_bytes` | `int` | `262144` | Max request body size (256KB) |
| `rate_limit_per_minute` | `int` | `30` | Sliding-window rate limit |

## Model Resolution

`ModelRegistry` (`ductor_bot/config.py`):

- Claude models are hardcoded: `haiku`, `sonnet`, `opus`.
- All non-Claude model IDs are treated as Codex IDs.
- `resolve_for_provider(model_name, available_providers)`:
  - uses native provider when available,
  - otherwise tries `_MODEL_EQUIVALENCE`,
  - otherwise falls back to any available provider (`opus` for Claude fallback, original model name for Codex fallback).

Current `_MODEL_EQUIVALENCE`:

- `opus` -> `gpt-5.2-codex`
- `sonnet` -> `gpt-5.1-codex-mini`
- `haiku` -> `gpt-5.1-codex-mini`
- `gpt-5.2-codex` -> `opus`
- `gpt-5.1-codex-max` -> `opus`
- `gpt-5.1-codex-mini` -> `sonnet`
- `gpt-5.2` -> `opus`
- `gpt-5.3-codex` -> `opus`

## Timezone Resolution

`resolve_user_timezone(configured)` in `ductor_bot/config.py`:

1. If `configured` (or `config.user_timezone`) is a valid IANA string -> use it.
2. Else try `$TZ` environment variable.
3. Else read `/etc/localtime` symlink target (Linux).
4. Else fall back to `UTC`.

Returns a `zoneinfo.ZoneInfo` instance. Used by:

- `CronObserver._schedule_job()` for cron expression interpretation.
- `SessionManager._is_fresh()` for `daily_reset_hour` boundary.
- `HeartbeatObserver._tick()` for quiet-hour evaluation.

Per-job override: `CronJob.timezone` takes precedence over global `user_timezone` when set.

## `reasoning_effort`

Valid values used by the model selector UI: `low`, `medium`, `high`, `xhigh`.

Flow:

`AgentConfig` -> `CLIServiceConfig` -> `CLIConfig` -> `CodexCLI._build_command()` (`-c model_reasoning_effort=<value>`).

Changed by model selector wizard and persisted to `config.json`.
