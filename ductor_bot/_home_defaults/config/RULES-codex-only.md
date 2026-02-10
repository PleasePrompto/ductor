# Config Directory

Runtime config lives here: `config.json`.
Edit only when the user asks for behavior changes.

## Safe Edit Workflow

1. Change only requested keys.
2. Preserve unrelated values and structure.
3. Never expose secrets (`telegram_token`, webhook tokens) in chat output.
4. Keep valid JSON.
5. Tell the user to run `/restart` after config changes.

## Important Key Groups

### Model and Provider

- `provider`: `codex`
- `model`: default model id
  - Available models:
    - `gpt-5.2-codex` - Frontier agentic coding model
    - `gpt-5.3-codex` - Latest frontier agentic coding model
    - `gpt-5.1-codex-max` - Codex-optimized for deep and fast reasoning
    - `gpt-5.2` - Latest frontier model
    - `gpt-5.1-codex-mini` - Cheaper, faster (limited reasoning)
- `reasoning_effort`: `low|medium|high|xhigh` (Codex models)
  - Most models support: `low`, `medium`, `high`, `xhigh`
  - `gpt-5.1-codex-mini` only: `medium`, `high`
- `permission_mode`: CLI permission behavior

### Time and Scheduling

- `user_timezone`: IANA timezone string (for example `Europe/Berlin`)
- `daily_reset_hour`: session reset boundary (in `user_timezone`)
- `heartbeat.quiet_start`, `heartbeat.quiet_end`: quiet hours (in `user_timezone`)
- `cleanup.check_hour`: daily cleanup hour (in `user_timezone`, not UTC)

If `user_timezone` is empty, runtime falls back to host timezone, then UTC.
For user-facing schedules, set `user_timezone` explicitly.

### Limits and Runtime

- `cli_timeout`
- `idle_timeout_minutes`
- `max_turns`, `max_budget_usd`, `max_session_messages`

### Streaming

- `streaming.enabled`
- `streaming.min_chars`, `streaming.max_chars`
- `streaming.idle_ms`, `streaming.edit_interval_seconds`
- `streaming.append_mode`, `streaming.sentence_break`

### Webhooks

- `webhooks.enabled`
- `webhooks.host`, `webhooks.port`
- `webhooks.token`
- `webhooks.max_body_bytes`, `webhooks.rate_limit_per_minute`

### Cleanup

- `cleanup.enabled`
- `cleanup.telegram_files_days`
- `cleanup.output_to_user_days`
- `cleanup.check_hour`

### File Sending Scope

- `file_access` controls what can be sent via `<file:...>`:
  - `all` (default)
  - `home`
  - `workspace`

### CLI Parameters

- `cli_parameters.codex`: List of extra CLI flags for Codex main agent (e.g., `["--chrome"]`)

These parameters are appended to every CLI invocation for the Codex provider.
Parameters are inserted before the `--` separator in commands.

**Example:**
```json
{
  "cli_parameters": {
    "codex": ["--chrome"]
  }
}
```

### Access Control

- `allowed_user_ids`
- `telegram_token`
