# cli/

Provider-agnostic CLI execution layer for Claude Code, Codex, Gemini, Antigravity, and Grok Build.

## Files

- `types.py`: `AgentRequest`, `AgentResponse`, `CLIResponse`
- `base.py`: `BaseCLI`, `CLIConfig`, `docker_wrap()`, Windows helpers
- `factory.py`: provider factory (`claude` / `codex` / `gemini` / `antigravity` / `grok`)
- `service.py`: `CLIService` gateway for orchestrator
- `init_wizard.py`: interactive onboarding and smart reset flow
- `executor.py`: shared subprocess lifecycle helpers for provider wrappers
- `timeout_controller.py`: configurable timeout warnings + activity-based extension controller
- `model_cache.py`: shared base classes for provider model-cache persistence and refresh observers
- `claude_provider.py`: Claude subprocess wrapper
- `codex_provider.py`: Codex subprocess wrapper
- `gemini_provider.py`: Gemini subprocess wrapper
- `antigravity_provider.py`: Antigravity (`agy`) subprocess wrapper — always runs on the host, even with the Docker sandbox enabled (the sandbox image ships no `agy` binary or auth state). `agy` has no headless streaming mode (`--print` is one-shot; `--prompt-interactive` needs a TTY), so `send_streaming` reuses the `--print` path. Because `agy --print` silently drops stdout in non-TTY subprocesses (upstream bug `google-antigravity/antigravity-cli#76`), the answer is read back from agy's own transcript (`<home>/.gemini/antigravity-cli/brain/<conv-id>/.system_generated/logs/transcript.jsonl`, the final `PLANNER_RESPONSE` entry — clean, without tool-call narration), with stdout as fallback. `--print <prompt>` is placed last and adjacent (it consumes the next token as its prompt value), and agy is grounded in the per-agent workspace via `--add-dir`
- `grok_provider.py`: Grok Build (`grok`) headless wrapper — oneshot `--output-format json`, streaming `streaming-json`, session `--resume`/`--continue`, long prompts via `--prompt-file`, tool filter via `--tools`/`--disallowed-tools`
- `stream_events.py`: normalized stream events + Claude stream parser
- `codex_events.py`: Codex JSONL parser
- `gemini_events.py`: Gemini NDJSON + JSON parser
- `antigravity_events.py`: Antigravity `--print` output parser (`parse_antigravity_json`)
- `grok_events.py`: Grok JSON / streaming-json parser (text, thought, tool, error, auto_compact_*, end)
- `coalescer.py`: streaming text coalescing buffer used by bot streaming dispatch
- `gemini_utils.py`: Gemini CLI discovery, trusted folder, model discovery helpers
- `codex_discovery.py`: Codex model discovery via `codex app-server` JSON-RPC
- `antigravity_discovery.py`: Antigravity model discovery via `agy models` (parses display names)
- `process_registry.py`: subprocess tracking/abort/kill
- `auth.py`: provider auth detection
- `param_resolver.py`: task override resolution for cron/webhook one-shot runs
- `codex_cache.py`, `codex_cache_observer.py`: Codex model cache + observer
- `gemini_cache.py`, `gemini_cache_observer.py`: Gemini model cache + observer
- `antigravity_cache.py`, `antigravity_cache_observer.py`: Antigravity model cache + observer (refreshes `~/.ductor/config/antigravity_models.json` from `agy models`, hourly)

## Execution path

1. Orchestrator builds `AgentRequest`.
2. `CLIService._make_cli()` resolves model/provider.
3. `CLIServiceConfig` injects provider-specific global CLI args.
4. `create_cli()` selects provider wrapper.
5. provider executes subprocess and returns `CLIResponse`.
6. service converts to `AgentResponse`.

Environment variables injected into CLI subprocesses:

- `DUCTOR_CHAT_ID`
- `DUCTOR_TOPIC_ID` (when set)
- `DUCTOR_TRANSPORT` (active transport identifier, e.g. `"tg"`, `"mx"`)
- `DUCTOR_AGENT_NAME`
- `DUCTOR_INTERAGENT_PORT`
- `DUCTOR_HOME`
- `DUCTOR_SHARED_MEMORY_PATH`
- `DUCTOR_TRANSCRIBE_COMMAND` / `DUCTOR_VIDEO_TRANSCRIBE_COMMAND` when external transcription hooks are configured

Host subprocesses additionally get:

- `DUCTOR_AGENT_ROLE` (`main` or `sub`)

Docker-wrapped subprocesses additionally get:

- `DUCTOR_INTERAGENT_HOST=host.docker.internal`

## Main-chat CLI parameters

Configured globally in `config.json`:

- `cli_parameters.claude`
- `cli_parameters.codex`
- `cli_parameters.gemini`
- `cli_parameters.antigravity`
- `cli_parameters.grok`

`CLIService` forwards them per provider.

## Task execution resolution (`param_resolver.py`)

Used by cron and webhook `cron_task` runs.

- input: `TaskOverrides(provider, model, reasoning_effort, cli_parameters)`
- output: immutable `TaskExecutionConfig`
- supported one-shot providers: `claude`, `codex`, `gemini`, `grok`
- validation:
  - Claude model in `CLAUDE_MODELS`
  - Codex model validated against `CodexModelCache`
  - Gemini model validated against aliases/discovered IDs or `gemini-*` patterns
  - Grok model in `GROK_MODELS` or `grok-*` prefix
- Codex / Claude / Grok reasoning effort applied only when supported by model
- task `cli_parameters` are appended after the global provider-specific args

## Streaming model

Normalized events in `stream_events.py` include:

- `AssistantTextDelta`
- `ToolUseEvent`
- `ToolResultEvent`
- `ThinkingEvent`
- `SystemStatusEvent`
- `CompactBoundaryEvent`
- `SystemInitEvent`
- `ResultEvent`

`CLIService.execute_streaming()` behavior:

- routes deltas/events to callbacks,
- forwards `CompactBoundaryEvent` through `on_compact_boundary` so the orchestrator can trigger silent memory-flush follow-up work,
- checks `ProcessRegistry.was_aborted(chat_id)` on each event,
- if stream fails or lacks final result event:
  - aborted -> empty result,
  - non-error with accumulated text -> use accumulated text,
  - else retry non-streaming and mark `stream_fallback=True`.

Timeout behavior in current production paths:

- provider wrappers accept both `timeout_seconds` and `timeout_controller`, and pass both into executor helpers.
- `SubprocessSpec.timeout_controller` is used in foreground and named-session flows where orchestrator builds controllers (`flows._make_timeout_controller`).
- when no controller is supplied, executor falls back to plain `asyncio.timeout(...)`.
- remaining timeout-only paths still using `timeout_seconds` include cron/webhook one-shot runs, inter-agent turns, and task-result/task-question injection turns.

Status-callback nuance:

- `TimeoutController` warning/extension callbacks are not currently wired to emit `SystemStatusEvent`s, so UI labels like `timeout_warning`/`timeout_extended` depend on future callback wiring.

`messenger/telegram/message_dispatch.py` wraps delta delivery with `StreamCoalescer` (`coalescer.py`) so Telegram edits flush at readable boundaries (paragraph/sentence/idle/full flush).

Session recovery is orchestrator-managed (`flows._recover_session`), not CLIService-managed.

Recovery triggers handled in orchestrator flows:

- SIGKILL termination (`returncode == -SIGKILL`)
- invalid resumed session (`"invalid session"` / `"session not found"` from provider CLI)

## Provider specifics

### Claude

- non-streaming uses `--output-format json`
- streaming uses `--output-format stream-json`
- respects `--max-turns`, `--max-budget-usd`, session resume/continue
- an `--append-system-prompt` value over 96 KiB would exceed the kernel per-argument limit (`execve` E2BIG), so it is written to a temp file and passed via `--append-system-prompt-file` instead (cleaned up after the run; in Docker mode the container-side path under the `/ductor` mount is passed)

### Codex

- fresh runs use `codex exec --json --color never --skip-git-repo-check`
- resumed runs use `codex exec resume [--json] -- <session_id>` and do not go through the same `--color never --skip-git-repo-check` path
- sandbox/approval flag selection from `permission_mode`
- reasoning effort via `-c model_reasoning_effort=...`
- `continue_session=True` is ignored for Codex

### Gemini

- command via `gemini` (or `node <index.js>` when resolved)
- non-streaming `--output-format json`, streaming `--output-format stream-json`
- permission bypass maps to `--approval-mode yolo`
- always includes `--include-directories .`
- trusts workspace path in `~/.gemini/trustedFolders.json`
- may inject `GEMINI_API_KEY` from ductor config when Gemini settings indicate API-key mode and no env key is set

### Grok Build

- binary `grok` (install: `curl -fsSL https://x.ai/cli/install.sh | bash`)
- non-streaming `--output-format json`, streaming `--output-format streaming-json`
- permission: `--permission-mode` plus `--always-approve` when `bypassPermissions`
- system prompt: `--system-prompt-override` / rules: `--rules`
- tool filter: `--tools` / `--disallowed-tools` (comma-separated built-in IDs; not permission globs)
- long prompts: `--prompt-file` (chat path and cron path)
- stream maps `error` → terminal error `ResultEvent`, `auto_compact_*` → `CompactBoundaryEvent` (memory flush)
- spend: prefers `usage.total_tokens` and maps `total_cost_usd` into `CLIResponse`
- models: discovered via `grok models` into `grok_models.json` (hourly refresh);
  fallback `grok-4.5` / `grok-composer-2.5-fast`; any `grok-*` ID still routes
- efforts: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`

## Auth detection (`auth.py`)

Statuses: `AUTHENTICATED`, `INSTALLED`, `NOT_FOUND`.

- Claude: `~/.claude/.credentials.json`
- Claude fallback paths: `ANTHROPIC_API_KEY`, then `claude auth status`
- Codex: `$CODEX_HOME/auth.json`
- Codex fallback paths: `OPENAI_API_KEY`; install markers: `version.json` or `config.toml`
- Gemini:
  - CLI presence (`find_gemini_cli`)
  - OAuth creds (`~/.gemini/oauth_creds.json`)
  - env/.env/API-key/Vertex markers
  - `settings.json` selected auth mode
  - optional fallback to `~/.ductor/config/config.json` `gemini_api_key`
- Grok: `~/.grok/auth.json`, then `XAI_API_KEY`, then `grok models` probe; install markers: binary or `config.toml`

## Model caches

Each provider's cache observer is created only when the startup auth detection reports that provider as installed (`installed_providers`). The detection is fallback-aware (e.g. finds a CLI under NVM that a plain PATH lookup would miss); providers without a detected CLI get no cache observer.

### Codex cache

- file: `~/.ductor/config/codex_models.json`
- discovery source: `discover_codex_models()` (`codex_discovery.py`) via `codex app-server` (`initialize` + `model/list`)
- loaded on startup with force refresh
- hourly refresh loop

### Gemini cache

- file: `~/.ductor/config/gemini_models.json`
- loaded on startup (uses cache when fresh, refreshes when stale/missing)
- hourly refresh loop
- refresh callback updates runtime Gemini model registry (`set_gemini_models`)

### Antigravity cache

- file: `~/.ductor/config/antigravity_models.json`
- discovery source: `discover_antigravity_models()` (`antigravity_discovery.py`) via `agy models`
- loaded on startup (uses cache when fresh, refreshes when stale/missing)
- hourly refresh loop
- refresh callback updates runtime Antigravity model registry (`set_antigravity_models`)
- the Telegram model selector currently offers only `antigravity-default` and
  explains that `agy` model selection is not reliable there; discovered names
  remain available for directive/API provider metadata.

### Grok cache

- file: `~/.ductor/config/grok_models.json` (per-agent home, e.g. `~/.ductor-cto/config/`)
- discovery source: `discover_grok_models()` (`grok_discovery.py`) via `grok models`
- loaded on startup (uses cache when fresh, refreshes when stale/missing)
- hourly refresh loop
- refresh callback updates runtime Grok model registry (`set_grok_models`)
- Telegram model selector shows discovery-ordered IDs via `get_grok_models_ordered()`

## Process registry

`ProcessRegistry` provides:

- registration/unregistration by chat with optional `topic_id` tracking
- `has_active(chat_id, topic_id=None)`: when `topic_id` is given, only processes for that specific topic are considered active; otherwise any process for the chat qualifies
- abort markers (`was_aborted`, `clear_abort`)
- `kill_all(chat_id)`
- `kill_for_task(task_id)` for background-task cancellation
- stale wall-clock cleanup (`kill_stale`)

`TaskHub` uses `kill_for_task(task_id)` before cancelling the asyncio task so streaming subprocess pipes unblock cleanly.

Windows uses process-tree termination (`taskkill /F /T`) to avoid orphaned child processes.

## Docker wrapping

`docker_wrap(cmd, config, extra_env=None, interactive=False)`:

- host mode (`config.docker_container == ""`): return original command + resolved local cwd
- container mode:
  - wraps command as `docker exec ... <container> ...`,
  - injects `DUCTOR_CHAT_ID`, optional `DUCTOR_TOPIC_ID`, `DUCTOR_TRANSPORT`, `DUCTOR_AGENT_NAME`, `DUCTOR_INTERAGENT_PORT`, `DUCTOR_HOME`, `DUCTOR_SHARED_MEMORY_PATH`, `DUCTOR_INTERAGENT_HOST`, and optional transcription hook vars,
  - merges user secrets from `~/.ductor/.env` (never overrides existing vars),
  - forwards optional env vars via `-e` flags (`extra_env`, overrides `.env`),
  - uses `-i` when `interactive=True` (required for stdin-fed providers like Gemini),
  - returns `cwd=None` (execution happens inside container context).
