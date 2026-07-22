# orchestrator/

Central routing layer between ingress transports (Telegram/Matrix/API) and CLI execution.

## Files

- `orchestrator/core.py`: `Orchestrator` class, command dispatch, integrations, thin delegations
- `orchestrator/lifecycle.py`: async factory/startup/shutdown helpers
- `orchestrator/observers.py`: `ObserverManager` for all observer lifecycle wiring
- `orchestrator/providers.py`: `ProviderManager` (provider auth/model resolution/metadata)
- `orchestrator/injection.py`: shared session injection for inter-agent and task flows
- `orchestrator/registry.py`: `CommandRegistry`, `OrchestratorResult`
- `orchestrator/commands.py`: slash-command handlers
- `orchestrator/flows.py`: normal/streaming/named-session/heartbeat flows
- `orchestrator/directives.py`: leading `@...` parser
- `orchestrator/hooks.py`: message hooks (`MAINMEMORY_REMINDER`, delegation hints)
- `orchestrator/memory_flush.py`: silent pre-compaction memory flush + optional `MAINMEMORY.md` compaction
- `orchestrator/selectors/*`: model/cron/session/task selector modules + selector types

## Why it was split

Recent refactors moved startup/provider/observer concerns out of `core.py`:

- lifecycle logic -> `lifecycle.py`
- provider/model state -> `providers.py`
- observer orchestration -> `observers.py`
- selector UI modules -> `orchestrator/selectors/` (transport-agnostic)

Result: smaller `core.py` with clearer responsibilities.

## Startup (`Orchestrator.create` -> `lifecycle.create_orchestrator`)

High-level steps:

1. resolve paths + set main-agent `DUCTOR_HOME`
2. optional Docker setup + Docker skill resync
3. runtime environment injection into workspace rule files
4. instantiate `Orchestrator` (sessions, CLI service, hook registry, optional memory flusher)
5. provider auth detection + available-provider update
6. initialize Gemini/Antigravity/Codex cache observers
7. initialize/start task observers (`Background`, `Cron`, `Webhook`) + `Heartbeat` + `Cleanup`
8. start rule/skill watcher tasks
9. optional API server startup
10. start config reloader

## Routing entry points

- `handle_message(key, text)`
- `handle_message_streaming(key, text, callbacks...)`

Common path:

1. clear abort marker for chat
2. suspicious input detection (log warning only)
3. command registry dispatch
4. directive parsing
5. normal or streaming flow

`is_chat_busy(chat_id, topic_id=None)` checks whether a CLI process is running. When `topic_id` is provided, only processes for that specific topic are considered busy; otherwise any process for the chat qualifies.

## `OrchestratorResult` metadata

`OrchestratorResult` carries optional metadata fields populated after CLI execution:

- `model_name`: resolved model identifier used for the turn
- `total_tokens`: total token count (input + output)
- `input_tokens`: input token count
- `cost_usd`: estimated cost in USD
- `duration_ms`: wall-clock execution time in milliseconds

## Command registry

Registered command handlers:

- `/new`, `/reset`, `/status`, `/model`, `/memory`, `/cron`, `/diagnose`, `/upgrade`, `/sessions`, `/tasks`

`/new` resets only the currently configured provider bucket for the active `SessionKey`.
Other provider buckets for the same chat/topic remain intact.

`/reset` resets the currently active provider bucket for the active `SessionKey`.

`/model` never blocks: it always executes immediately (bypasses the sequential queue) and shows current model info if a CLI process is active in the chat.

Runtime main-agent registration:

- `/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`

Not orchestrator-owned:

- `/stop`, `/stop_all` (middleware/bot layer)

## Provider/model management (`ProviderManager`)

Responsibilities:

- authenticated provider set
- runtime model->provider resolution
- known-model IDs for directive parsing
- API provider metadata for auth responses

Directive resolution supports:

- provider directives (`@codex`, `@gemini`, `@claude`, `@antigravity`, `@grok`)
- model directives (`@opus`, `@flash`, cache-backed IDs)

## Selector subsystem (`orchestrator/selectors/`)

Selectors are transport-agnostic and return `SelectorResponse` with abstract button types.

Modules:

- `model_selector.py` (`ms:*`)
- `cron_selector.py` (`crn:*`)
- `session_selector.py` (`nsc:*`)
- `task_selector.py` (`tsc:*`)
- shared models/utilities in `selectors/models.py`, `selectors/utils.py`

Important model-selector behavior:

- topic-scoped `/model` changes only the active topic session
- non-topic `/model` updates config defaults (and sub-agent `agents.json` when relevant)

## Flow behavior (`flows.py`)

### Normal/streaming

- session resolution via `SessionKey`
- provider-isolated session buckets
- optional new-session system prompt append (`MAINMEMORY.md`)
- in-flight foreground turn tracking
- `CompactBoundaryEvent` marks a pre-compaction boundary; after a successful user turn the optional `MemoryFlusher` may run a silent follow-up flush turn and, when configured, a compaction turn
- single automatic recovery retry on SIGKILL/invalid resumed session
- stale-session detection currently covers CLI errors such as `invalid session`, `session not found`, and `no conversation found`
- session update on success
- successful tool-only / empty turns are converted into a neutral visible status line instead of disappearing

### Named session flow

- named session registry lookup/resume
- foreground follow-up support (`@name <msg>`)

### Heartbeat flow

- read-only session lookup
- provider match + cooldown checks
- ACK-token suppression

## Injection paths (`injection.py`)

Shared helper `_inject_prompt(...)` is used by:

- `handle_async_interagent_result(...)`
- `handle_task_result(...)`
- `handle_task_question(...)`
- public `inject_prompt(...)` on orchestrator

All injection paths respect `topic_id` when provided.

Inter-agent ingress also lives in `injection.py`:

- deterministic named sessions use scoped `ia.<sender-slug>.t<topic>.x<hash>` names, or legacy `ia-<sender>` without source context
- if the provider CLI rejects a stored inter-agent session after an update/cache clear, the orchestrator ends that named session, retries once with a fresh session, and surfaces a visible recovery notice back to the caller

## Observer and bus wiring

`Orchestrator.wire_observers_to_bus(bus, wake_handler=...)`:

- delegates to `ObserverManager.wire_to_bus(...)`
- sets bus injector to orchestrator
- replaces old per-observer setter scatter

Observer manager owns lifecycle for:

- background, cron, webhook, heartbeat, cleanup
- Gemini/Antigravity/Codex cache observers
- config reloader
- rule sync watcher
- skill sync watcher

## Config hot-reload impact

`_on_config_hot_reload(...)` updates orchestrator-owned runtime services for hot fields:

- CLI defaults (`model`, `provider`, `max_turns`, `max_budget_usd`, `permission_mode`, `reasoning_effort`, provider CLI args)
- provider known-model cache refresh when `model` changes
- i18n re-init when `language` changes
- heartbeat observer start/stop when `heartbeat.enabled` flips
- external bot callback for transport-owned hot fields such as auth/group updates

`ConfigReloader` classifies additional top-level fields as hot globally, but fields such as `notifications`, `transcription`, and memory-maintenance settings still require restart today because they are not emitted as orchestrator hot updates.

Restart-required fields are surfaced via reloader callback logging.

## API integration

`lifecycle.start_api_server(...)`:

- generates API token when missing
- computes default chat fallback from `api.chat_id` or first allowed user
- wires streaming message handler, abort handler, file context, provider metadata, active-state getter

`ApiServer` supports auth payload `channel_id` -> topic-aware `SessionKey`.

## Shutdown

`lifecycle.shutdown(...)` performs:

1. kill active CLI processes
2. stop API server (if running)
3. cleanup managed skill links
4. stop observers/reloader/cache/watchers
5. Docker teardown (if enabled)
