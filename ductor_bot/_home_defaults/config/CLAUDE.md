# Config Directory

This directory contains runtime configuration for the bot.

## Purpose

- Main file: `config.json`
- Source defaults: framework `config.example.json` (outside user home)

Edit config only when the user explicitly asks for behavior changes.

## Safe Edit Rules

1. Change only the requested keys.
2. Preserve existing structure and unrelated values.
3. Never expose secrets (`telegram_token`, `webhooks.token`) in chat output.
4. After changes, tell the user to run `/restart` so config reloads.

## Key Groups (Quick Reference)

### Model and Provider

- `provider`: `claude` or `codex`
- `model`: active model id/name
- `reasoning_effort`: Codex thinking level (`low`/`medium`/`high`/`xhigh`)
- `permission_mode`: CLI permission behavior

### Timezone

- `user_timezone`: IANA timezone string (e.g. `"Europe/Berlin"`, `"America/New_York"`). Default: `""` (empty = host system timezone, then UTC). Affects cron schedules, daily session reset, and heartbeat quiet hours. **Set this when the user creates their first cron job.**

### Limits and Timeouts

- `cli_timeout`
- `idle_timeout_minutes`
- `daily_reset_hour` (interpreted in `user_timezone`)
- `max_turns`, `max_budget_usd`, `max_session_messages`

### Streaming Output

- `streaming.enabled`
- `streaming.min_chars`, `streaming.max_chars`
- `streaming.idle_ms`, `streaming.edit_interval_seconds`
- `streaming.append_mode`, `streaming.sentence_break`

### Heartbeat

- `heartbeat.enabled`
- `heartbeat.interval_minutes`, `heartbeat.cooldown_minutes`
- `heartbeat.quiet_start`, `heartbeat.quiet_end`
- `heartbeat.prompt`, `heartbeat.ack_token`

### Webhooks

- `webhooks.enabled`
- `webhooks.host`, `webhooks.port`
- `webhooks.token`
- `webhooks.max_body_bytes`, `webhooks.rate_limit_per_minute`

### Docker (Optional)

- `docker.enabled`
- `docker.image_name`, `docker.container_name`
- `docker.auto_build`

### Access Control

- `allowed_user_ids`: whitelist for Telegram users
- `telegram_token`: bot token
