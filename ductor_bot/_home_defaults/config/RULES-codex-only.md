# Config Directory

Runtime config lives here: `config.json`.
Edit only when the user asks for behavior changes.

## Safe Edit Workflow

1. Change only requested keys.
2. Preserve unrelated values and structure.
3. Never expose secrets (`telegram_token`, webhook tokens) in chat output.
4. Keep valid JSON.
5. Most settings are hot-reloadable (take effect within seconds). Only `telegram_token`, `docker`, `api`, `webhooks`, `log_level`, and `gemini_api_key` require `/restart`.

## Important Key Groups

### Model and Provider

- `provider`: `codex`
- `model`: default model id
  - Available models:
    - `gpt-5.5` - Newest/recommended frontier agentic coding model
    - `gpt-5.4` - Frontier agentic coding model
    - `gpt-5.4-mini` - Smaller, faster frontier coding model
    - `gpt-5.3-codex-spark` - Codex-optimized coding model
- `reasoning_effort`: `low|medium|high|xhigh` (Codex models)
  - Supported values: `low`, `medium`, `high`, `xhigh`
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
- `cleanup.media_files_days`
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

### Language

- `language`: UI language for bot messages — `en`, `de`, `nl`, `fr`, `ru`, `es`, `pt`
- Hot-reloadable: change without restart.

### Image Processing

- `image.max_dimension`: max width/height in pixels (default `2000`). Images larger than this are resized.
- `image.output_format`: target format — `webp` (default), `png`, or `jpeg`
- `image.quality`: compression quality 1-100 (default `85`)
- Incoming images from all transports are auto-converted after download.
- Hot-reloadable.

### Scene Indicators

- `scene.seen_reaction`: `true`/`false` (default `false`) — show a 👀 reaction on received messages
- `scene.technical_footer`: `true`/`false` (default `false`) — append model name, token count, cost, and duration to the final response
- Hot-reloadable.

### Heartbeat

- `heartbeat.enabled`: `true`/`false` — master switch
- `heartbeat.interval_minutes`: global tick interval for private chats (default `30`)
- `heartbeat.quiet_start`, `heartbeat.quiet_end`: quiet hours (0-23, in `user_timezone`)
- `heartbeat.prompt`: the prompt sent to the agent during heartbeat
- `heartbeat.ack_token`: token the agent replies with when nothing to report (default `HEARTBEAT_OK`)
- `heartbeat.group_targets`: list of per-group/topic heartbeat targets, each with:
  - `enabled`, `chat_id`, `topic_id`, `prompt`, `ack_token`, `interval_minutes`, `quiet_start`, `quiet_end`
  - Each target with its own `interval_minutes` runs independently from the global tick.
  - Set `enabled: false` to pause a target without removing it.
- Heartbeat is hot-reloadable.

### Timeouts

- `timeouts.normal`: max seconds for a normal CLI call (default `600`)
- `timeouts.background`: max seconds for background tasks (default `1800`)
- `timeouts.subagent`: max seconds for sub-agent tasks (default `3600`)
- `timeouts.extend_on_activity`: auto-extend timeout when tool activity detected

### Tasks

- `tasks.enabled`: `true`/`false` — enable background task system
- `tasks.max_parallel`: max concurrent background tasks (default `5`)
- `tasks.timeout_seconds`: default timeout per task

### Access Control

- `allowed_user_ids`
- `allowed_group_ids`
- `group_mention_only`: when `true`, bot only responds in groups when @mentioned or replied to
- `telegram_token`
