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

- `provider`: `claude`, `codex`, `gemini`, or `antigravity`
- `model`: default model id
  - Claude models: `haiku`, `sonnet`, `sonnet[1m]`, `opus`, `opus[1m]`, `fable`
  - Codex models:
    - `gpt-5.5` - Newest/recommended frontier agentic coding model
    - `gpt-5.4` - Frontier agentic coding model
    - `gpt-5.4-mini` - Smaller, faster frontier coding model
    - `gpt-5.3-codex-spark` - Codex-optimized coding model
  - Gemini models:
    - `gemini-2.5-pro` - Balanced, most capable
    - `gemini-2.5-flash` - Fast and cost-effective
    - `gemini-3-pro-preview` - Next-gen preview
    - `gemini-3.1-pro-preview` - Latest pro preview
    - `gemini-3.1-flash-lite` - Lightweight fast preview
    - `gemini-3.5-flash` - Latest fast model
  - Antigravity models:
    - `antigravity-default` - Let `agy` choose the provider-level default
    - Display names from `config/antigravity_models.json` may be known at runtime,
      but the chat model selector currently exposes only `antigravity-default`
      because `agy` model selection is not reliable there
- `reasoning_effort`: reasoning depth for Codex and Claude
  - Codex: `low`, `medium`, `high`, `xhigh`
  - Claude: `low`, `medium`, `high`, `xhigh`, `max`
  - Change it for the active provider with the `/effort` command
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

### System Prompt

- `append_system_prompt_files`: list of workspace-relative filenames whose
  contents are appended to the system prompt on every agent-driven turn
  (e.g. `["GLOSSARY.md", "STYLE_GUIDE.md"]`). Empty (default) injects nothing.
  Each agent reads its own workspace copy; missing files are skipped.

### File Sending Scope

- `file_access` controls what can be sent via `<file:...>`:
  - `all` (default)
  - `home`
  - `workspace`

### CLI Parameters

- `cli_parameters.claude`: List of extra CLI flags for Claude main agent (e.g., `["--chrome"]`)
- `cli_parameters.codex`: List of extra CLI flags for Codex main agent (e.g., `["--chrome"]`)
- `cli_parameters.gemini`: List of extra CLI flags for Gemini main agent
- `cli_parameters.antigravity`: List of extra CLI flags for Antigravity main agent (e.g., `["--log-file", "agy.log"]`)

These parameters are appended to every CLI invocation for the respective provider.
Parameters are inserted before the `--` separator in commands.

**Example:**
```json
{
  "cli_parameters": {
    "claude": ["--chrome"],
    "codex": ["--chrome"],
    "gemini": ["--sandbox"],
    "antigravity": ["--log-file", "agy.log"]
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

- `scene.seen_reaction`: `true`/`false` (default `false`) — show a 👀 reaction on received messages (Telegram: emoji reaction, Matrix: read receipt)
- `scene.technical_footer`: `true`/`false` (default `false`) — append model name, token count, cost, and duration to the final response
- Hot-reloadable.

### Heartbeat

- `heartbeat.enabled`: `true`/`false` — master switch
- `heartbeat.interval_minutes`: global tick interval for private chats (default `30`)
- `heartbeat.cooldown_minutes`: min gap between heartbeats per chat
- `heartbeat.quiet_start`, `heartbeat.quiet_end`: quiet hours (0-23, in `user_timezone`)
- `heartbeat.prompt`: the prompt sent to the agent during heartbeat
- `heartbeat.ack_token`: token the agent replies with when nothing to report (default `HEARTBEAT_OK`)
- `heartbeat.group_targets`: list of per-group/topic heartbeat targets, each with:
  - `enabled`: `true`/`false` — toggle this target without removing it
  - `chat_id`: Telegram group ID (use `null` for template entries that are skipped)
  - `topic_id`: forum topic ID within the group (or `null` for the whole group)
  - `prompt`: override prompt for this target (or `null` to use global)
  - `ack_token`: override ack token (or `null` to use global)
  - `interval_minutes`: independent interval for this target (or `null` to use global). Each target with its own interval runs in its own loop, independent of the global tick.
  - `quiet_start`, `quiet_end`: override quiet hours (or `null` to use global)

**Example:**
```json
{
  "heartbeat": {
    "enabled": true,
    "interval_minutes": 30,
    "group_targets": [
      {
        "enabled": true,
        "chat_id": -1001234567890,
        "topic_id": 5,
        "prompt": "Check server status and report anomalies.",
        "interval_minutes": 5
      },
      {
        "enabled": false,
        "chat_id": -1009876543210,
        "prompt": "Paused target — enable when ready."
      }
    ]
  }
}
```

Heartbeat is hot-reloadable. Changing `enabled` from false to true starts the observer without restart.

### Timeouts

- `timeouts.normal`: max seconds for a normal CLI call (default `600`)
- `timeouts.background`: max seconds for background tasks (default `1800`)
- `timeouts.subagent`: max seconds for sub-agent tasks (default `3600`)
- `timeouts.extend_on_activity`: auto-extend timeout when tool activity detected
- `timeouts.activity_extension`: seconds to extend per activity burst
- `timeouts.max_extensions`: max number of auto-extensions

### Tasks

- `tasks.enabled`: `true`/`false` — enable background task system
- `tasks.max_parallel`: max concurrent background tasks (default `5`)
- `tasks.timeout_seconds`: default timeout per task

### Access Control

- `allowed_user_ids`
- `allowed_group_ids`
- `group_mention_only`: when `true`, bot only responds in groups when @mentioned or replied to (applies to both text and media messages)
- `telegram_token`
