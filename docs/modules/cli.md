# cli/

Provider-agnostic CLI layer for Claude Code and Codex. Owns subprocess execution, stream normalization, auth checks, process tracking, provider-specific CLI parameter routing, and Codex model-cache primitives.

## Files

- `types.py`: `AgentRequest`, `AgentResponse`, `CLIResponse`.
- `base.py`: `BaseCLI` interface, `CLIConfig`, `docker_wrap()`.
- `factory.py`: provider factory (`ClaudeCodeCLI` or `CodexCLI`).
- `service.py`: `CLIService` gateway used by orchestrator.
- `claude_provider.py`: async Claude CLI wrapper.
- `codex_provider.py`: async Codex CLI wrapper.
- `stream_events.py`: normalized stream events + Claude stream-json parser.
- `codex_events.py`: Codex JSONL parsing + normalized stream event conversion.
- `coalescer.py`: stream text coalescing helper.
- `process_registry.py`: active subprocess tracking and termination.
- `auth.py`: provider auth-state detection.
- `param_resolver.py`: `TaskOverrides` + `TaskExecutionConfig` resolution for cron/webhook `cron_task` runs.
- `codex_cache.py`: persistent Codex model cache (`CodexModelCache`).
- `codex_cache_observer.py`: background cache lifecycle (`CodexCacheObserver`).
- `codex_discovery.py`: low-level Codex discovery via `codex app-server` JSON-RPC.

## Public API (`cli/__init__.py`)

- service: `CLIService`, `CLIServiceConfig`
- providers/base: `BaseCLI`, `CLIConfig`, `create_cli`
- request/response: `AgentRequest`, `AgentResponse`, `CLIResponse`
- streaming helper: `StreamCoalescer`, `CoalesceConfig`
- process/auth: `ProcessRegistry`, `check_all_auth`, `AuthResult`, `AuthStatus`

## Request Path (main chat, non-streaming)

1. Orchestrator builds `AgentRequest`.
2. `CLIService._make_cli()` resolves `(model, provider)` from request + available providers.
3. Service maps provider to CLI parameters via `CLIServiceConfig.cli_parameters_for_provider(provider)`.
4. Factory creates provider wrapper with `CLIConfig`.
5. Provider executes subprocess and returns `CLIResponse`.
6. Service converts to `AgentResponse`.

## CLI Parameter Routing

Main-agent CLI flags are configured per provider:

- `CLIServiceConfig.claude_cli_parameters`
- `CLIServiceConfig.codex_cli_parameters`

`CLIService._make_cli()` injects them into `CLIConfig.cli_parameters`. Both providers append `cli_parameters` **before** the `--` separator.

## Task Execution Resolution (cron/webhook)

`param_resolver.py` provides a shared resolution path for unattended task runs:

- `TaskOverrides`: optional per-task fields (`provider`, `model`, `reasoning_effort`, `cli_parameters`).
- `TaskExecutionConfig`: resolved immutable execution settings.
- `resolve_cli_config(base_config, codex_cache, task_overrides=...)`:
  - provider/model fallback to global config when override is missing,
  - Claude model validation against hardcoded set (`haiku`, `sonnet`, `opus`),
  - Codex model validation against `CodexModelCache`,
  - reasoning effort allowed only for Codex models that support it,
  - task `cli_parameters` passed through to command builders.

Current behavior: task-level `cli_parameters` are task-specific (no merge with global `AgentConfig.cli_parameters`).

## Codex Model Cache

`CodexModelCache` (`codex_cache.py`):

- persisted as JSON (`last_updated`, model metadata, supported efforts, defaults),
- `load_or_refresh(cache_path)` uses cache when fresh (`<24h`) and re-discovers when missing/stale/corrupt,
- `_refresh_and_save()` writes atomically (`.tmp` + replace),
- discovery source: `discover_codex_models()` in `codex_discovery.py`.

`CodexCacheObserver` (`codex_cache_observer.py`):

- loads cache at startup,
- runs an hourly loop and re-runs `load_or_refresh()`,
- exposes `get_cache()` for orchestrator/model-selector/diagnose paths.

## Stream Event Types

Normalized events parsed from Claude `stream-json` output (`stream_events.py`):

- `AssistantTextDelta`: text content chunks.
- `ToolUseEvent`: tool invocation indicator.
- `ResultEvent`: final result with session ID, cost, usage.
- `SystemInitEvent`: session initialization with `session_id`.
- `SystemStatusEvent`: status changes (e.g. `status="compacting"` for context compaction start, `status=null` for end).
- `CompactBoundaryEvent`: context compaction boundary marker with `trigger` (`auto`/`manual`) and `pre_tokens`.
- `ThinkingEvent`: reasoning/thinking block events.

Codex emits no client-side compaction events (handled server-side). Codex streaming applies `CodexThinkingFilter`: text emitted directly before tool events is buffered/dropped to reduce noisy stream output.

## Streaming Path

`CLIService.execute_streaming()`:

1. consume provider stream events via `_StreamCallbacks`,
2. forward text deltas/tool events/system status to callbacks,
3. capture final `ResultEvent`.

Fallback handling:

- if stream exception or no `ResultEvent`:
  - aborted chat (`ProcessRegistry.was_aborted`) -> empty result,
  - accumulated text without stream error -> return accumulated text,
  - otherwise retry non-streaming `execute()` and mark `stream_fallback=True`.

## Provider Command Behavior

### Claude (`ClaudeCodeCLI`)

Base command:

```bash
claude -p --output-format json \
  --permission-mode <mode> \
  --model <model> \
  [--system-prompt ...] [--append-system-prompt ...] \
  [--max-turns ...] [--max-budget-usd ...] \
  [--resume <session_id> | --continue] \
  [<cli_parameters...>] \
  -- <prompt>
```

Streaming mode:

- switches `--output-format` to `stream-json`,
- adds `--verbose`.

### Codex (`CodexCLI`)

Base command:

```bash
codex exec --json --color never --skip-git-repo-check \
  <sandbox_flags> [--model ...] [-c model_reasoning_effort=...] \
  [--instructions ...] [--image ...] [<cli_parameters...>] \
  -- <final_prompt>
```

Prompt composition for Codex:

- `system_prompt` + user prompt + `append_system_prompt` are merged into one prompt body.

Resume behavior:

- resume uses `codex exec resume ...`.
- `continue_session=True` is ignored for Codex (debug-log only).

## Process Registry

`ProcessRegistry` responsibilities:

- `register(chat_id, process, label)` / `unregister(...)`
- `kill_all(chat_id)` with SIGTERM -> grace period -> SIGKILL -> reap
- abort marker API: `was_aborted()` / `clear_abort()`
- activity check: `has_active(chat_id)`
- `kill_stale(max_age_seconds)`: kills wall-clock-stale processes (`time.time()`), used by heartbeat after suspend/resume scenarios.

Each `TrackedProcess` records `registered_at` (wall clock).

## Auth Detection (`auth.py`)

- Claude authenticated: `~/.claude/.credentials.json`
- Codex authenticated: `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`)
- Codex installed fallback check: `$CODEX_HOME/version.json`

Status enum:

- `AUTHENTICATED`
- `INSTALLED`
- `NOT_FOUND`

## Docker Wrapping

`docker_wrap(cmd, docker_container, chat_id, working_dir)`:

- no container: run locally with `cwd=working_dir`.
- with container: `docker exec -e DUCTOR_CHAT_ID=<id> <container> ...`, `cwd=None`.

## Model Discovery Note

`codex_discovery.py` is now a low-level source for cache refresh. The `/model` wizard reads models from `CodexModelCache` (through orchestrator), not from live discovery per request.

## Key Design Choices

- Single service boundary (`CLIService`) for orchestrator calls.
- Normalized stream events decouple bot/orchestrator from provider JSON formats.
- Shared `param_resolver` keeps unattended execution logic in one place.
- Cached Codex model metadata removes repeated discovery latency from the model selector.
- Centralized subprocess control (`ProcessRegistry`) supports robust abort and stale-process cleanup.
