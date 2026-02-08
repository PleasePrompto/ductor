# cli/

Provider-agnostic CLI layer for Claude Code and Codex. Owns subprocess execution, stream normalization, auth checks, process tracking, and fallback logic.

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
- `codex_discovery.py`: Codex model discovery via `codex app-server` JSON-RPC.

## Public API (`cli/__init__.py`)

- service: `CLIService`, `CLIServiceConfig`
- providers/base: `BaseCLI`, `CLIConfig`, `create_cli`
- request/response: `AgentRequest`, `AgentResponse`, `CLIResponse`
- streaming helper: `StreamCoalescer`, `CoalesceConfig`
- process/auth: `ProcessRegistry`, `check_all_auth`, `AuthResult`, `AuthStatus`

## Request Path (non-streaming)

1. Orchestrator builds `AgentRequest`.
2. `CLIService._make_cli()` resolves `(model, provider)` from request + available providers.
3. Factory creates provider wrapper.
4. Provider executes subprocess and returns `CLIResponse`.
5. Service converts to `AgentResponse`.

## Stream Event Types

Normalized events parsed from Claude `stream-json` output (`stream_events.py`):

- `AssistantTextDelta`: text content chunks.
- `ToolUseEvent`: tool invocation indicator.
- `ResultEvent`: final result with session ID, cost, usage.
- `SystemInitEvent`: session initialization with `session_id`.
- `SystemStatusEvent`: status changes (e.g. `status="compacting"` for context compaction start, `status=null` for end).
- `CompactBoundaryEvent`: context compaction boundary marker with `trigger` (`auto`/`manual`) and `pre_tokens` count.
- `ThinkingEvent`: reasoning/thinking block events.

Codex emits no client-side compaction events (handled server-side).
Codex streaming additionally applies `CodexThinkingFilter`: assistant text emitted right before tool events is buffered and dropped so only meaningful response text reaches the Telegram stream.

## Streaming Path

`CLIService.execute_streaming()`:

1. consume provider stream events via `_StreamCallbacks` dispatcher,
2. forward text deltas, tool events, and system status through callbacks,
3. `SystemStatusEvent` -> `on_system_status` callback (compaction indicator),
4. `CompactBoundaryEvent` -> logged at INFO level + `on_system_status(None)` to clear indicator,
5. capture final `ResultEvent`.

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
  <prompt>
```

Streaming mode:

- switches to `--output-format stream-json`,
- adds `--verbose`.

### Codex (`CodexCLI`)

Base command:

```bash
codex exec --json --color never --skip-git-repo-check \
  <sandbox_flags> [--model ...] [-c model_reasoning_effort=...] \
  [--instructions ...] [--image ...] \
  <final_prompt>
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
- `kill_stale(max_age_seconds)`: kills processes older than threshold in wall-clock time (`time.time()`). Used by heartbeat to clean up processes that survived system suspend (where monotonic-based `asyncio.timeout` did not fire).

Each `TrackedProcess` records `registered_at` (wall-clock) for stale detection.

This registry is shared across providers and consumed by middleware/orchestrator/heartbeat.

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

`codex_discovery.py` returns dynamic Codex model metadata for the `/model` wizard. If discovery fails, the wizard shows no Codex models (no static fallback list is injected there).

## Key Design Choices

- One service boundary (`CLIService`) for all orchestrator calls.
- Normalized stream events decouple bot/orchestrator from provider-specific JSON formats.
- Centralized subprocess control (`ProcessRegistry`) for robust abort/cleanup behavior.
