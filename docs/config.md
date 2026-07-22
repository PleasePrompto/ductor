# Configuration

Runtime config file: `~/.ductor/config/config.json`.

Seed source: `<repo>/config.example.json` (source checkout) or packaged fallback `ductor_bot/_config_example.json` (installed mode).

## Config Creation

Primary path: `ductor onboarding` (interactive wizard) writes `config.json` with user-provided values merged into `AgentConfig` defaults.

## Load & Merge Behavior

Config is merged in two places:

1. `ductor_bot/__main__.py::load_config()`
   - creates config on first start (copy from `config.example.json` or Pydantic defaults),
   - deep-merges runtime file with `AgentConfig` defaults,
   - writes back only when new keys were added.
2. `ductor_bot/workspace/init.py::_smart_merge_config()`
   - shallow merge `{**defaults, **existing}` with `config.example.json`,
   - preserves existing user top-level keys,
   - fills missing top-level keys from `config.example.json`.

Normalization detail:

- onboarding and runtime config load normalize `gemini_api_key` default to string `"null"` in persisted JSON for backward compatibility.
- `AgentConfig` validator converts null-like text (`""`, `"null"`, `"none"`) to `None` at runtime.

Runtime edits persisted through config helpers include `/model` changes (model/provider/reasoning), webhook token auto-generation, and API token auto-generation.

API config persistence note:

- `load_config()` intentionally does not auto-add the `api` block during default deep-merge (beta gating).
- `ductor api enable` writes the `api` block (including generated token) into `config.json`.

## External API Secrets (`~/.ductor/.env`)

User-defined environment secrets for external APIs (e.g. `PPLX_API_KEY`, `DEEPSEEK_API_KEY`).

Standard dotenv syntax:

```env
PPLX_API_KEY=sk-xxx
DEEPSEEK_API_KEY=sk-yyy
export MY_VAR="quoted value"
```

Propagation:

- host CLI execution: merged into subprocess env via `_build_subprocess_env()`
- Docker exec: injected as `-e` flags via `docker_wrap()`
- Docker container creation: injected as `-e` flags via `_start_container()`
- sub-agents and background tasks: inherited through the same execution paths

Priority (highest to lowest):

1. existing host environment variables (never overridden)
2. provider-specific config (e.g. `gemini_api_key` in `config.json`)
3. `.env` values (fill gaps only)

Changes take effect on the next CLI invocation (mtime-based cache invalidation, no restart needed).

## `AgentConfig` (`ductor_bot/config.py`)

| Field | Type | Default | Notes |
|---|---|---|---|
| `log_level` | `str` | `"INFO"` | Applied at startup unless CLI `--verbose` is used |
| `provider` | `str` | `"claude"` | Default provider |
| `model` | `str` | `"opus"` | Default model ID |
| `ductor_home` | `str` | `"~/.ductor"` | Runtime home root |
| `idle_timeout_minutes` | `int` | `1440` | Session freshness idle timeout (`0` disables idle expiry) |
| `session_age_warning_hours` | `int` | `12` | Adds `/new` reminder after threshold (every 10 messages) |
| `daily_reset_hour` | `int` | `4` | Daily reset boundary hour in `user_timezone` |
| `daily_reset_enabled` | `bool` | `false` | Enables daily session reset checks |
| `user_timezone` | `str` | `""` | IANA timezone used by cron/heartbeat/cleanup/session reset |
| `language` | `str` | `"en"` | UI language for onboarding, commands, status text, and chat-facing system messages |
| `max_budget_usd` | `float \| None` | `None` | Passed to Claude CLI |
| `max_turns` | `int \| None` | `None` | Passed to Claude CLI |
| `max_session_messages` | `int \| None` | `None` | Session rollover limit |
| `permission_mode` | `str` | `"bypassPermissions"` | Provider sandbox/approval mode |
| `cli_timeout` | `float` | `1800.0` | Legacy/global timeout. Still used by cron/webhook `cron_task`, inter-agent turns, stale-process heartbeat cleanup, and as fallback for unknown timeout paths |
| `reasoning_effort` | `str` | `"medium"` | Default reasoning effort for Claude (`--effort`) and Codex (`-c model_reasoning_effort`); per-session override via `/effort` |
| `append_system_prompt_files` | `list[str]` | `[]` | Workspace-relative files appended to the system prompt on every agent-driven turn (chat, named sessions, inter-agent, tasks); paths escaping the workspace and files over 256 KiB are skipped |
| `project_roots` | `dict[str, str]` | `{}` | Per-topic working-directory override: maps a topic key to a directory the CLI runs in instead of the shared workspace (see below) |
| `file_access` | `str` | `"all"` | File access scope (`all`, `home`, `workspace`) for file sends and API `GET /files`; unknown values fall back to workspace-only |
| `gemini_api_key` | `str \| None` | `None` | Config fallback key injected for Gemini API-key mode |
| `transport` | `str` | `"telegram"` | Messaging transport: `"telegram"` or `"matrix"` |
| `transports` | `list[str]` | `[]` | List of transports to run in parallel (e.g. `["telegram", "matrix"]`). When empty, falls back to single `transport` value. |
| `telegram_token` | `str` | `""` | Telegram bot token (required when `transport=telegram`) |
| `allowed_user_ids` | `list[int]` | `[]` | Telegram user allowlist (applies in both private and group chats) |
| `allowed_group_ids` | `list[int]` | `[]` | Telegram group allowlist (which groups the bot can operate in; default `[]` = no groups, fail-closed). In groups, both the group and the user must be allowlisted |
| `allowed_channel_ids` | `list[int]` | `[]` | Telegram channel allowlist for join/audit behavior; unauthorized channels are auto-left |
| `group_mention_only` | `bool` | `false` | Mention/reply gating in group rooms. Telegram: filter only (no auth bypass). Matrix: in non-DM rooms this bypasses `allowed_users` and uses room + mention/reply as gate |
| `matrix` | `MatrixConfig` | see below | Matrix homeserver connection (required when `transport=matrix`) |
| `streaming` | `StreamingConfig` | see below | Streaming tuning |
| `docker` | `DockerConfig` | see below | Docker sidecar config |
| `heartbeat` | `HeartbeatConfig` | see below | Background heartbeat config |
| `memory_flush` | `MemoryFlushConfig` | see below | Silent pre-compaction memory flush after streaming compact boundaries |
| `memory_reflection` | `MemoryReflectionConfig` | see below | Optional periodic memory reflection hook |
| `memory_compaction` | `MemoryCompactionConfig` | see below | LLM-driven `MAINMEMORY.md` compaction policy |
| `cleanup` | `CleanupConfig` | see below | Daily file-retention cleanup |
| `webhooks` | `WebhookConfig` | see below | Webhook HTTP server config |
| `api` | `ApiConfig` | see below | Direct WebSocket API server config |
| `cli_parameters` | `CLIParametersConfig` | see below | Provider-specific extra CLI flags |
| `image` | `ImageConfig` | see below | Incoming image processing settings |
| `timeouts` | `TimeoutConfig` | see below | Path-specific timeout policy (`normal`, `background`, `subagent`) |
| `tasks` | `TasksConfig` | see below | Delegated background task system (`TaskHub`) |
| `cron_delivery_retry` | `CronDeliveryRetryConfig` | see below | Opt-in resend of preserved cron results after a delivery failure |
| `cron_preflight` | `CronPreflightConfig` | see below | Opt-in task-local gate that can skip a cron agent run |
| `scene` | `SceneConfig` | see below | Scene indicators and technical footer |
| `notifications` | `NotificationsConfig` | see below | Targeted startup/upgrade notification routing |
| `transcription` | `TranscriptionConfig` | see below | External audio/video transcription command hooks |
| `skills` | `SkillsConfig` | see below | Cross-tool skill sync toggles (global + per-provider) |
| `update_check` | `bool` | `true` | Enables periodic update observer (`UpdateObserver`) |
| `interagent_port` | `int` | `8799` | Port for internal localhost API (`InternalAgentAPI`) |

### Multi-transport behavior

When `transports` is empty (default), the single `transport` value
is used. When `transports` contains multiple entries (e.g.
`["telegram", "matrix"]`), `MultiBotAdapter` starts all listed
transports in parallel and `transport` is auto-set to the first
entry. A model validator normalizes both fields at load time so
they stay consistent.

### `project_roots`

Maps a forum topic to a working directory the CLI runs in instead of the shared
workspace (`resolve_project_root` in `workspace/project_roots.py`, wired through
`Orchestrator._resolve_request_working_dir` and applied at the single
`CLIService._make_cli` choke point).

Keys are tried in priority order; the first that maps to an existing directory wins:

1. the human-readable topic name (as shown in Telegram),
2. `"<chat_id>:<topic_id>"` (disambiguates equal topic ids across chats),
3. `"<topic_id>"` (plain topic id).

```json
{
  "project_roots": {
    "backend": "~/code/backend",
    "-1001234567890:42": "~/code/secret-service",
    "99": "~/code/scratch"
  }
}
```

Behavior:

- only applies inside a topic (`topic_id` set); the general chat and named sessions (`ns:` — resume consistency) always keep the default workspace.
- skipped in Docker mode: `docker_wrap` maps cwd into the container relative to the workspace and would fail on an outside path.
- when an override is active, a note is appended to the system prompt telling the agent to address bot memory (`memory_system/MAINMEMORY.md`) by its absolute workspace path, so a relative reference does not land inside the user's project repo.
- hot-reloadable.

Security note: topic **names** are set by anyone with Telegram's "Manage Topics" right, so in a multi-admin group a name key can be claimed by renaming an unrelated topic. Prefer `"<chat_id>:<topic_id>"` keys for sensitive roots.

## `MatrixConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `homeserver` | `str` | `""` | Matrix homeserver URL (e.g. `https://matrix.org`) |
| `user_id` | `str` | `""` | Bot user ID (e.g. `@ductor:matrix.org`) |
| `password` | `str` | `""` | Password for initial login |
| `access_token` | `str` | `""` | Optional manual restore source; runtime normally persists credentials in the Matrix store |
| `device_id` | `str` | `""` | Optional manual restore source paired with `access_token` |
| `allowed_rooms` | `list[str]` | `[]` | Room IDs or aliases the bot may operate in |
| `allowed_users` | `list[str]` | `[]` | Matrix user IDs allowed to interact |
| `store_path` | `str` | `"matrix_store"` | E2EE key store directory, relative to `ductor_home` |

Notes:

- first successful login persists credentials to `~/.ductor/<store_path>/credentials.json` (mode `0o600`), not back into `config.json`
- when `access_token` and `device_id` are explicitly present in `config.json`, runtime restores from them and also mirrors them into the credentials store
- The bot supports end-to-end encrypted rooms via `matrix-nio[e2e]`.
- `allowed_rooms` and `allowed_users` together form the Matrix auth model.

## `CLIParametersConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `claude` | `list[str]` | `[]` | Extra args appended to Claude CLI command |
| `codex` | `list[str]` | `[]` | Extra args appended to Codex CLI command |
| `gemini` | `list[str]` | `[]` | Extra args appended to Gemini CLI command |
| `antigravity` | `list[str]` | `[]` | Extra args appended to Antigravity (`agy`) CLI command |
| `grok` | `list[str]` | `[]` | Extra args appended to Grok Build (`grok`) CLI command |

Used by `CLIServiceConfig` for main-chat calls.

Argument shape note:

- each list element is passed as one CLI argument; do not combine multiple shell flags into one string such as `"--verbose --chrome"`

Automation note:

- cron/webhook `cron_task` runs merge global provider-specific `cli_parameters` first, then task-level `cli_parameters` from `cron_jobs.json` / `webhooks.json`.

## `TimeoutConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `normal` | `float` | `600.0` | Default timeout for foreground chat turns (`normal` / `normal_streaming`) |
| `background` | `float` | `1800.0` | Timeout for named background sessions (`BackgroundObserver`) |
| `subagent` | `float` | `3600.0` | Reserved timeout bucket for sub-agent-specific paths |
| `warning_intervals` | `list[float]` | `[60.0, 10.0]` | Warning thresholds for `TimeoutController` |
| `extend_on_activity` | `bool` | `true` | Enables deadline extension when subprocess output is active |
| `activity_extension` | `float` | `120.0` | Seconds added per granted extension |
| `max_extensions` | `int` | `3` | Maximum activity-based extensions |

Runtime sync behavior:

- `AgentConfig` keeps backward compatibility with `cli_timeout`.
- If `cli_timeout != 600.0` and `timeouts.normal` is still default, runtime validation copies `cli_timeout` into `timeouts.normal`.
- If `timeouts.normal` is explicitly set, it wins over `cli_timeout`.

Current execution-path usage:

- foreground chat turns: `resolve_timeout(config, "normal")` -> `timeouts.normal`
- named background sessions (`/session`): `timeouts.background`
- delegated background tasks (`TaskHub`): `tasks.timeout_seconds`
- cron + webhook `cron_task`: still `config.cli_timeout`
- inter-agent turns: still `config.cli_timeout`
- stale-process cleanup threshold: `config.cli_timeout * 2`

Implementation status note:

- `cli/timeout_controller.py` and warning/extension config are implemented and tested.
- provider wrappers and executor support `TimeoutController` in production paths.
- normal/streaming/named-session/heartbeat flows create controllers via `flows._make_timeout_controller(...)`.
- timeout warning/extension callbacks are not yet wired to Telegram/API system-status output, so user-visible timeout status labels are not emitted by default.

## `TasksConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `true` | Enables shared delegated task system (`TaskHub`) |
| `max_parallel` | `int` | `5` | Max concurrent running tasks per chat in `TaskHub` |
| `timeout_seconds` | `float` | `3600.0` | Timeout per delegated task run |
| `finished_retention_hours` | `int` | `168` | Age limit for finished task history (done/failed/cancelled); `0` disables age pruning |
| `finished_keep_last` | `int` | `100` | Max finished tasks kept (newest first); `0` disables count pruning. Age and count are independent limits |

## `CronDeliveryRetryConfig`

Opt-in resend of cron results whose delivery failed, without re-running the agent (`#160` delivery tracking).

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | When `true`, `CronObserver` starts a background retry sweep at startup |
| `interval_seconds` | `int` | `300` | Sweep interval and delay between attempts (`>= 1`) |
| `max_attempts` | `int` | `12` | Max retry attempts per job before the preserved result is abandoned (`>= 1`) |

Behavior:

- only jobs with `last_delivery_status == "failed"` and a preserved `last_result_text` are retried; a job currently executing is skipped so a retry never races a fresh run.
- at-least-once semantics: a successful retry only clears the preserved result when it still matches the text that was resent (a newer failed result landing mid-flight is kept for the next sweep).
- restart-required: toggling `enabled` starts/stops the sweep loop, so it does not hot-reload.

## `CronPreflightConfig`

Opt-in deterministic gate that can skip a scheduled agent run before the CLI subprocess is spawned.

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | When `true`, each cron run first checks for a task-local preflight script |
| `timeout_seconds` | `float` | `15.0` | Preflight subprocess timeout (`> 0`) |
| `skip_marker` | `str` | `"HEARTBEAT_OK"` | Last non-empty stdout line that signals "skip the agent run" |

Behavior:

- script path: `cron_tasks/<task_folder>/scripts/preflight.py`, run with `sys.executable` in the task folder (`DUCTOR_HOME` exported).
- skip decision: preflight exits `0` and its last non-empty stdout line equals `skip_marker` → the agent is not run (status `success:preflight`, delivery status `skipped`).
- fail-open: a missing script, a spawn failure (EMFILE/ENOMEM/permissions), a timeout, or a nonzero/`2` exit or non-empty stderr all let the agent run anyway; the gate never suppresses a run on its own error.
- timeout kill is process-group-wide on POSIX (`start_new_session` + `killpg`) so grandchildren cannot survive.

## Task-Level Automation Overrides

Stored outside `config.json` in:

- `~/.ductor/cron_jobs.json` (`CronJob`)
- `~/.ductor/webhooks.json` (`WebhookEntry`, `cron_task` mode)

Common per-task fields:

- execution: `provider`, `model`, `reasoning_effort`, `cli_parameters`
- scheduling guards: `quiet_start`, `quiet_end`, `dependency`

Cron-only field:

- `timezone` (per-job timezone override)

Behavior notes:

- missing execution fields fall back to global config via `resolve_cli_config()`,
- `dependency` is global across cron + webhook `cron_task` runs (shared `DependencyQueue`),
- quiet-hour checks run only when per-task quiet fields are set (no fallback to global heartbeat quiet settings).

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
| `show_reasoning_stream` | `bool` | `false` |
| `show_tool_progress` | `bool` | `true` |
| `show_thinking_indicator` | `bool` | `true` |

- `show_reasoning_stream`: when the provider exposes reasoning/thinking blocks, stream them as separate Telegram messages instead of degrading them to a plain status indicator.
- `show_tool_progress`: show live Telegram tool activity messages during streaming turns.
- `show_thinking_indicator`: keep the lighter Telegram `THINKING` status indicator when no reasoning stream is being shown.

## `DockerConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `image_name` | `str` | `"ductor-sandbox"` | Docker image name |
| `container_name` | `str` | `"ductor-sandbox"` | Docker container name |
| `auto_build` | `bool` | `true` | Build image automatically when missing |
| `mount_host_cache` | `bool` | `false` | Mount host `~/.cache` into container (see below) |
| `mounts` | `list[str]` | `[]` | Extra host directories mounted into sandbox (`/mnt/...`) |
| `extras` | `list[str]` | `[]` | Optional AI/ML package IDs to install in the Docker image (see below) |

`Orchestrator.create()` calls `DockerManager.setup()` when enabled. If setup fails, ductor logs warning and falls back to host execution.

### `mount_host_cache`

Mounts the host's platform-specific cache directory into the container at `/home/node/.cache`:

| Platform | Host path |
|---|---|
| Linux | `~/.cache` (or `$XDG_CACHE_HOME`) |
| macOS | `~/Library/Caches` |
| Windows | `%LOCALAPPDATA%` |

Use case: browser-based skills (e.g. google-ai-mode) that use patchright/playwright need access to persistent browser profiles and browser binaries stored in the host cache. Without this, each container start requires a fresh CAPTCHA solve and Chrome download.

Disabled by default because it exposes the host cache directory to the sandbox.

### `mounts`

User-defined directory mounts for project/data access inside Docker sandbox.

- each entry is expanded (`~`, env vars), resolved, and validated as an existing directory
- each entry is just a host directory path (for example `"/home/you/projects"`), not Docker `host:container[:mode]` syntax
- invalid or missing entries are skipped with warnings
- container target path is derived from host basename: `/mnt/<sanitized-name>`
- duplicate target names are disambiguated as `/mnt/name_2`, `/mnt/name_3`, ...

Runtime note:

- updates are typically managed via `ductor docker mount|unmount`
- changing mounts requires bot restart (or `ductor docker rebuild`) to affect container run flags

### `extras`

Optional AI/ML packages installed into the Docker sandbox image at build time. Each entry is an ID from the extras registry (`ductor_bot/infra/docker_extras.py`).

Available extras:

| ID | Name | Category | Size |
|---|---|---|---|
| `ffmpeg` | FFmpeg | Audio / Speech | ~100 MB |
| `whisper` | Faster Whisper | Audio / Speech | ~500 MB |
| `opencv` | OpenCV | Vision / OCR | ~100 MB |
| `tesseract` | Tesseract OCR | Vision / OCR | ~40 MB |
| `easyocr` | EasyOCR | Vision / OCR | ~2.5 GB |
| `pymupdf` | PyMuPDF | Document Processing | ~50 MB |
| `pandoc` | Pandoc | Document Processing | ~80 MB |
| `scipy` | SciPy | Scientific / Data | ~130 MB |
| `pandas` | pandas | Scientific / Data | ~60 MB |
| `matplotlib` | Matplotlib | Scientific / Data | ~60 MB |
| `pytorch-cpu` | PyTorch (CPU) | ML Frameworks | ~800 MB |
| `transformers` | HF Transformers | ML Frameworks | ~2 GB |
| `playwright` | Playwright | Web / Browser | ~450 MB |

Dependency resolution:

- `whisper` depends on `ffmpeg`
- `easyocr` and `transformers` depend on `pytorch-cpu`
- dependencies are auto-resolved at build time

Managed via `ductor docker extras-add|extras-remove` or during onboarding wizard. Changes require `ductor docker rebuild` to take effect.

When extras are configured, the supervisor startup timeout is dynamically extended to accommodate longer Docker build times.

## `HeartbeatConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `interval_minutes` | `int` | `30` | Loop interval |
| `cooldown_minutes` | `int` | `5` | Skip if user active recently |
| `quiet_start` | `int` | `21` | Quiet start hour in `user_timezone` |
| `quiet_end` | `int` | `8` | Quiet end hour in `user_timezone` |
| `prompt` | `str` | default prompt | Multiline default prompt references `MAINMEMORY.md` and `cron_tasks/` |
| `ack_token` | `str` | `"HEARTBEAT_OK"` | Suppression token |
| `group_targets` | `list[HeartbeatTarget]` | placeholder list | Runtime default is one disabled placeholder target so new configs show the expected shape immediately |

### `HeartbeatTarget`

Each entry in `group_targets` identifies a specific group chat (and optional topic) to send heartbeat checks to. All optional fields override the global `HeartbeatConfig` when set; unset fields fall back to global values.

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `enabled` | `bool` | no | `true` | Target-level master toggle |
| `chat_id` | `int` | yes | | Target group chat ID |
| `topic_id` | `int \| None` | no | `None` | Optional forum topic ID within the group |
| `prompt` | `str \| None` | no | `None` | Per-target prompt override (falls back to global `prompt`) |
| `ack_token` | `str \| None` | no | `None` | Per-target suppression token (falls back to global `ack_token`) |
| `interval_minutes` | `int \| None` | no | `None` | Per-target interval override (falls back to global `interval_minutes`) |
| `quiet_start` | `int \| None` | no | `None` | Per-target quiet-hour start (falls back to global `quiet_start`) |
| `quiet_end` | `int \| None` | no | `None` | Per-target quiet-hour end (falls back to global `quiet_end`) |

## `MemoryFlushConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `true` | Enables the post-stream silent flush pipeline |
| `flush_prompt` | `str` | default prompt | Prompt appended as a silent follow-up turn to capture durable facts before memory compaction |
| `dedup_seconds` | `int` | `300` | In-memory dedup window per `SessionKey` to avoid repeated flushes on back-to-back compact boundaries |

Runtime behavior:

- `CompactBoundaryEvent` from the provider stream marks the session for flush
- after the user-visible streaming turn succeeds, `MemoryFlusher.maybe_flush(...)` resumes the same CLI session silently
- errors are logged and swallowed; memory maintenance never blocks the user reply

## `MemoryReflectionConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Registers the reflection hook only when enabled |
| `every_n_messages` | `int` | `10` | Hook cadence; fires on the N-th outbound prompt for a session |
| `prompt` | `str` | default prompt | Silent reflection prompt appended through `MessageHookRegistry` |

Runtime behavior:

- implemented as a normal message hook, not as a background observer
- complements the always-on `MAINMEMORY_REMINDER` hook rather than replacing it

## `MemoryCompactionConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `true` | Enables LLM-driven compaction after a flush when file size threshold is exceeded |
| `trigger_lines` | `int` | `70` | `MAINMEMORY.md` line-count threshold that makes compaction eligible |
| `target_lines` | `int` | `40` | Target post-compaction size used in the prompt template |
| `preserve_recency_days` | `int` | `14` | Recent entries to preserve verbatim during compaction |
| `prompt` | `str` | default prompt template | Formatted at runtime with `target_lines` and `preserve_days` |

Compaction runs only after a successful flush and reuses the same provider session as the user turn.

## `ImageConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `max_dimension` | `int` | `2000` | Maximum width/height in pixels; images exceeding this are resized proportionally |
| `output_format` | `str` | `"webp"` | Target image format (e.g. `webp`, `jpeg`, `png`) |
| `quality` | `int` | `85` | Compression quality for lossy formats (WebP, JPEG) |

Applied to incoming images across all transports (Telegram, Matrix, API). See `files/image_processor.py` for implementation details.

## `SceneConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `seen_reaction` | `bool` | `false` | Enables "seen" indicator on incoming messages (Telegram: emoji reaction, Matrix: read receipt) |
| `status_reaction` | `bool` | `true` | Telegram-only stage tracker on the user message while the turn runs; when enabled it wins over `seen_reaction` so both do not fight over the same emoji slot |
| `technical_footer` | `bool` | `false` | Appends model/token/cost/time footer to agent responses |

## `NotificationsConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `startup_targets` | `list[NotificationTarget]` | `[]` | When non-empty and containing at least one enabled target with `chat_id`, startup notices are routed only there |
| `upgrade_targets` | `list[NotificationTarget]` | `[]` | Same routing rule for update-available notices |

### `NotificationTarget`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `true` | Target-level toggle |
| `chat_id` | `int \| None` | `None` | Telegram chat ID or Matrix room-mapped int |
| `topic_id` | `int \| None` | `None` | Telegram forum topic; ignored by Matrix |

Behavior notes:

- empty target lists preserve the old broadcast-to-all behavior
- Telegram uses dedicated `notify_startup(...)` / `notify_upgrade(...)` helpers
- Matrix currently implements targeted startup routing only; upgrade-target routing remains Telegram-specific

## `TranscriptionConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `audio_command` | `str` | `""` | When set, exported as `DUCTOR_TRANSCRIBE_COMMAND` for `tools/media_tools/transcribe_audio.py` |
| `video_command` | `str` | `""` | When set, exported as `DUCTOR_VIDEO_TRANSCRIBE_COMMAND` for `tools/media_tools/process_video.py` |

Empty strings keep the bundled fallback chain intact:

- audio: external hook -> OpenAI Whisper API -> local `whisper` CLI -> `whisper.cpp`
- video: external hook -> existing built-in video transcription path

## `CleanupConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `true` | Master toggle |
| `media_files_days` | `int` | `30` | Retention for media files (telegram + matrix) |
| `output_to_user_days` | `int` | `30` | Retention in `workspace/output_to_user/` |
| `api_files_days` | `int` | `30` | Retention in `workspace/api_files/` |
| `check_hour` | `int` | `3` | Local hour in `user_timezone` for cleanup run |

Cleanup implementation detail:

- cleanup is recursive (`_delete_old_files` walks nested files via `rglob("*")`),
- after file deletion, empty subdirectories are pruned,
- dated upload folders (`.../YYYY-MM-DD/...`) are cleaned when contained files exceed retention and directories become empty.

## `WebhookConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `host` | `str` | `"127.0.0.1"` | Bind address (localhost by default) |
| `port` | `int` | `8742` | HTTP server port |
| `token` | `str` | `""` | Global bearer fallback token (auto-generated when webhooks start) |
| `max_body_bytes` | `int` | `262144` | Max request body size |
| `rate_limit_per_minute` | `int` | `30` | Sliding-window rate limit |

## `ApiConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `host` | `str` | `"0.0.0.0"` | Bind address |
| `port` | `int` | `8741` | API HTTP/WebSocket port |
| `token` | `str` | `""` | Bearer/WebSocket auth token (generated by `ductor api enable`, with runtime generation fallback on API start) |
| `chat_id` | `int` | `0` | Default API session chat (`0` means fallback to first `allowed_user_ids` entry, else `1`) |
| `allow_public` | `bool` | `false` | Suppresses Tailscale-not-detected warning |

Runtime note (`Orchestrator._start_api_server` + `ApiServer._authenticate`):

- `config.api.chat_id` is used via truthiness (`0` falls back),
- fallback default comes from first `allowed_user_ids` entry (fallback `1`),
- per-connection auth payload may override via:
  - `{"type":"auth","chat_id":...}` (positive int),
  - optional `channel_id` (positive int) for per-channel session isolation (`SessionKey.topic_id`),
- clients can override only for that connection; persisted default stays in config.

## `SkillsConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `sync_enabled` | `bool` | `true` | Global toggle for cross-tool skill sync; when `false`, `sync_skills` returns early and no sync runs |
| `sync` | `SkillSyncProviders` | see below | Per-provider sync toggles |

### `SkillSyncProviders`

| Field | Type | Default | Notes |
|---|---|---|---|
| `claude` | `bool` | `true` | Include `~/.claude/skills` in cross-tool sync |
| `codex` | `bool` | `true` | Include `~/.codex/skills` (or `$CODEX_HOME/skills`) in cross-tool sync |
| `gemini` | `bool` | `true` | Include `~/.gemini/skills` in cross-tool sync |
| `grok` | `bool` | `true` | Include `~/.grok/skills` in cross-tool sync |

Toggles are read live from `config.json` on each skill-sync tick (independent of `ConfigReloader`), so changes take effect within one sync interval without restart. A disabled provider is dropped from the sync, so its skill dir is neither linked into nor used as a source; existing ductor-created links are not actively removed (cleared on shutdown cleanup or manually).

## Runtime hot-reload (`config_reload.py`)

`Orchestrator.create()` starts `ConfigReloader`, which polls `config.json` every 5 seconds, validates it with `AgentConfig`, diffs top-level fields, and applies safe changes without restart.

Hot-reloadable top-level fields:

- `model`, `provider`, `reasoning_effort`
- `cli_timeout`, `max_budget_usd`, `max_turns`, `max_session_messages`
- `idle_timeout_minutes`, `session_age_warning_hours`, `daily_reset_hour`, `daily_reset_enabled`
- `permission_mode`, `file_access`, `user_timezone`
- `streaming`, `heartbeat`, `cleanup`, `cli_parameters`, `scene`, `image`, `language`
- `project_roots` (per-topic working-dir overrides take effect on the next turn)
- `allowed_user_ids`, `allowed_group_ids`, `group_mention_only`

Current non-hot fields that often surprise people:

- `allowed_channel_ids` exists on `AgentConfig` but is not currently classified as hot-reloadable by `ConfigReloader`, so channel allowlist changes still require restart
- `notifications`, `transcription`, `timeouts`, and `tasks` are restart-required

Observer lifecycle caveat:

- heartbeat/cleanup values hot-reload into config
- observer start/stop is not hot-toggled
- enabling heartbeat/cleanup after startup requires restart if the observer was not started initially

Restart-required top-level fields:

- `transport`, `telegram_token`, `matrix`
- `docker`, `api`, `webhooks`
- `ductor_home`, `log_level`, `gemini_api_key`, `notifications`, `transcription`, `timeouts`, `tasks`
- `cron_delivery_retry`, `cron_preflight` (both start/stop or re-gate observer behavior at startup)

Restart classification is computed from `AgentConfig` top-level schema fields.

## Model Resolution

`ModelRegistry` (`ductor_bot/config.py`):

- Claude models are hardcoded: `haiku`, `sonnet`, `sonnet[1m]`, `opus`, `opus[1m]`, and `fable` (Claude CLI strips the `[1m]` suffix and sets the 1M-context beta header internally).
- Gemini aliases are hardcoded: `auto`, `pro`, `flash`, `flash-lite`.
- Runtime Gemini models are discovered from local Gemini CLI files at startup.
- Antigravity has a built-in `antigravity-default` model and runtime model display names discovered from `agy models`.
  The Telegram `/model` selector currently exposes only `antigravity-default`
  because `agy` model selection is not reliable there; discovered display names
  are still known to directives and API provider metadata.
- Grok Build models have a hardcoded fallback list (`grok-4.5`, `grok-composer-2.5-fast`) and refresh from `grok models` at startup (discovery order preserved). The Telegram `/model` selector shows the discovery-ordered IDs via `get_grok_models_ordered()`.
- Provider resolution (`provider_for(model_id)`):
  - Claude when in `CLAUDE_MODELS` or when model looks like `claude-*`,
  - Gemini when in aliases/discovered set or when model looks like `gemini-*`/`auto-gemini-*`,
  - Antigravity when in the built-in/discovered set or when model looks like `antigravity-*`,
  - Grok when in `GROK_MODELS`/discovered set or when model looks like `grok-*`,
  - otherwise Codex.

## Timezone Resolution

`resolve_user_timezone(configured)` in `ductor_bot/config.py`:

1. valid configured IANA timezone,
2. `$TZ` env var,
3. host system detection:
   - Windows: local datetime tzinfo,
   - POSIX: `/etc/localtime` symlink,
4. fallback `UTC`.

Returns `ZoneInfo` when available, otherwise a UTC tzinfo fallback object with `key="UTC"` on systems without timezone data. Used by cron scheduling, session daily-reset checks, heartbeat quiet hours, and cleanup scheduling.

## `reasoning_effort`

UI values: `low`, `medium`, `high`, `xhigh` (Codex and Claude); Claude additionally supports `max`. Grok Build accepts a wider headless set (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`) applied via `--reasoning-effort`.

Effort is resolved **per session**: each forum topic can run at its own effort
(`/effort` or the `/model` thinking-level step), the main chat / DM sets the
global default. Codex resumes re-assert both model and effort.

Main-chat flow:

`AgentConfig` -> session capture -> `AgentRequest.effort_override` -> `CLIConfig` -> `ClaudeCLI` (`--effort <value>`) / `CodexCLI` (`-c model_reasoning_effort=<value>`).

Automation flow:

- `resolve_cli_config()` applies reasoning effort only for models that support the requested effort.

## Codex Model Cache

Path: `~/.ductor/config/codex_models.json`.

Behavior:

- loaded at orchestrator startup (`CodexCacheObserver.start()`),
- startup load is forced refresh (`force_refresh=True`),
- checked hourly in background,
- `load_or_refresh()` uses cache if `<24h` old, otherwise re-discovers via Codex app server,
- consumed by `/model` wizard, `resolve_cli_config()` for cron/webhook validation, and `/diagnose` output.

## Gemini Model Cache

Path: `~/.ductor/config/gemini_models.json`.

Behavior:

- loaded at orchestrator startup (`GeminiCacheObserver.start()`),
- startup load uses cached data when fresh and refreshes only when stale/missing,
- refreshed hourly in background,
- refresh callback updates runtime Gemini model registry (`set_gemini_models(...)`) used by directives and model selector.

## Antigravity Model Cache

Path: `~/.ductor/config/antigravity_models.json`.

Behavior:

- loaded at orchestrator startup (`AntigravityCacheObserver.start()`),
- startup load uses cached data when fresh and refreshes only when stale/missing,
- refreshed hourly in background,
- refresh callback updates runtime Antigravity model registry (`set_antigravity_models(...)`) used by directives and API provider metadata.
- the Telegram `/model` selector intentionally offers only `antigravity-default`
  and displays the current `agy` model-selection limitation.

## Grok Model Cache

Path: `~/.ductor/config/grok_models.json` (per-agent home).

Behavior:

- loaded at orchestrator startup (`GrokCacheObserver.start()`),
- startup load uses cached data when fresh and refreshes only when stale/missing,
- refreshed hourly in background,
- refresh callback updates the runtime Grok model registry (`set_grok_models(...)`), preserving discovery order for the `/model` selector.

## Model-cache observer gating

The Gemini, Antigravity, Grok, and Codex cache observers are only created for providers found by the startup auth detection (`installed_providers`), which is fallback-aware (e.g. finds a Gemini CLI under NVM that a plain PATH lookup would miss). A provider whose CLI is not detected gets no cache observer at all.

## `agents.json` (Multi-Agent Registry)

Path: `~/.ductor/agents.json`.

Top-level JSON array of `SubAgentConfig` objects. Each entry defines a sub-agent that runs alongside the main agent.

Managed via:

- `ductor agents add <name>` (interactive CLI, currently Telegram-focused)
- `ductor agents remove <name>` (CLI)
- `create_agent.py` / `remove_agent.py` tool scripts (from within a CLI session)
- manual file editing (auto-detected by `FileWatcher`)

### `SubAgentConfig` fields

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `name` | `str` | yes | | Unique lowercase identifier |
| `transport` | `str` | no | `"telegram"` | `"telegram"` or `"matrix"` |
| `telegram_token` | `str` | conditional | | Required when `transport=telegram` |
| `matrix` | `MatrixConfig` | conditional | | Required when `transport=matrix` |
| `allowed_user_ids` | `list[int]` | no | `[]` | Telegram user allowlist |
| `allowed_group_ids` | `list[int]` | no | `[]` | Telegram group allowlist |
| `group_mention_only` | `bool` | no | inherited | Mention/reply gating toggle (transport-specific behavior) |
| `provider` | `str` | no | inherited | Default provider |
| `model` | `str` | no | inherited | Default model |
| `log_level` | `str` | no | inherited | |
| `idle_timeout_minutes` | `int` | no | inherited | |
| `session_age_warning_hours` | `int` | no | inherited | |
| `daily_reset_hour` | `int` | no | inherited | |
| `daily_reset_enabled` | `bool` | no | inherited | |
| `max_budget_usd` | `float` | no | inherited | |
| `max_turns` | `int` | no | inherited | |
| `max_session_messages` | `int` | no | inherited | |
| `permission_mode` | `str` | no | inherited | |
| `cli_timeout` | `float` | no | inherited | |
| `reasoning_effort` | `str` | no | inherited | |
| `file_access` | `str` | no | inherited | |
| `streaming` | `StreamingConfig` | no | inherited | |
| `docker` | `DockerConfig` | no | inherited | |
| `heartbeat` | `HeartbeatConfig` | no | inherited | |
| `cleanup` | `CleanupConfig` | no | inherited | |
| `webhooks` | `WebhookConfig` | no | inherited | |
| `api` | `ApiConfig` | no | disabled | Disabled by default for sub-agents |
| `cli_parameters` | `CLIParametersConfig` | no | inherited | |
| `user_timezone` | `str` | no | inherited | |

"inherited" means the value comes from the main agent's `config.json` when omitted.

Timeout nuance:

- `SubAgentConfig` currently has no dedicated `timeouts` field.
- `SubAgentConfig` currently has no dedicated `tasks` field.
- `SubAgentConfig` also has no dedicated `scene`, `notifications`, `transcription`, `language`, or `allowed_channel_ids` fields.
- sub-agents inherit the main agent `timeouts` block through merge base.
- sub-agents inherit the main agent `tasks` block through merge base.
- the same inheritance currently applies to `scene`, `notifications`, `transcription`, `language`, and `allowed_channel_ids`.

Example:

```json
[
  {
    "name": "researcher",
    "telegram_token": "123456:ABC...",
    "allowed_user_ids": [12345678],
    "provider": "claude",
    "model": "sonnet"
  },
  {
    "name": "coder",
    "transport": "matrix",
    "matrix": {
      "homeserver": "https://matrix.example.com",
      "user_id": "@coder:example.com",
      "password": "...",
      "allowed_rooms": ["!room:example.com"],
      "allowed_users": ["@user:example.com"]
    },
    "provider": "codex",
    "reasoning_effort": "high"
  }
]
```

### Sub-agent runtime merge behavior

`merge_sub_agent_config(main, sub, agent_home)` builds the effective sub-agent `AgentConfig` with this priority:

1. main agent config (`config.json`) as base
2. explicit non-null overrides from `agents.json` (highest priority)

Then it always forces:

- `ductor_home = ~/.ductor/agents/<name>/`
- `transport`, `telegram_token`, `matrix`, `allowed_user_ids`, and `allowed_group_ids` from the sub-agent entry
- `api.enabled = false` when no explicit `api` block is provided

Notes:

- there is no extra persisted runtime config layer for sub-agents in merge order
- `/model` changes in a sub-agent chat are written back to `agents.json`, so restart/reload uses the updated values from that registry file

### `agents.json` watcher behavior

`AgentSupervisor` watches `agents.json` (mtime poll every 5s):

- new entry -> start sub-agent
- removed entry -> stop sub-agent
- restart triggers for running agents:
  - `transport` changed
  - Telegram identity changed (`telegram_token`)
  - Matrix identity changed (`matrix.homeserver` or `matrix.user_id`)
- other field changes currently do not auto-restart running agents

For non-token field updates on a running agent, use `/agent_restart <name>` (or restart the bot) to apply them immediately.
