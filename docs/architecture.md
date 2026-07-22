# Architecture

## Runtime Overview

ductor supports multiple messaging transports. The `transport` config
field (`"telegram"` or `"matrix"`) selects a single ingress/delivery
layer via a transport registry (`messenger/registry.py`). The
`transports` list field enables parallel multi-transport execution
(e.g. `["telegram", "matrix"]`); when empty it falls back to the
single `transport` value.

```text
Telegram path:                          Matrix path:
  Telegram Update                         Matrix sync event
  -> aiogram Dispatcher/Router            -> matrix-nio callback
  -> AuthMiddleware                       -> room/user allowlist check
  -> SequentialMiddleware                 -> MatrixBot handler
  -> TelegramBot handler                  -> Orchestrator
  -> Orchestrator                         -> CLIService
  -> CLIService                           -> provider subprocess
  -> provider subprocess                  -> Matrix room message
  -> Telegram message (stream edits)

Background/async results (both transports):
  -> Observer/TaskHub/InterAgentBus callback
  -> bus.adapters -> Envelope
  -> MessageBus
  -> optional lock + optional session injection
  -> transport-specific delivery (TelegramTransport or MatrixTransport)
```

When `transports` lists more than one entry, `MultiBotAdapter`
starts all transports in parallel and exposes a unified
`BotProtocol` to the orchestrator.

Direct API path (`api.enabled=true`) uses `ApiServer` and calls orchestrator streaming callbacks directly.

### Transport dispatch

`messenger/registry.py` maps `config.transport` to a bot factory:

- `"telegram"` -> `TelegramBot` (aiogram)
- `"matrix"` -> `MatrixBot` (matrix-nio)

Both implement `BotProtocol`. Adding a new transport requires only a new factory entry.

## Startup Flow

### `ductor` entry (`ductor_bot/__main__.py`)

1. parse CLI args and dispatch command (implementation in `cli_commands/*`)
2. default run path:
   - `_is_configured()` only checks the minimal onboarding gate for active transports
   - Telegram gate: non-placeholder token + non-empty `allowed_user_ids`
   - Matrix gate: non-empty `homeserver` + non-empty `user_id`
   - deeper transport validation (Matrix password/access token + allowlists) happens later in `_validate_*_config()`
   - if not configured: onboarding (includes transport selection)
   - load/deep-merge config (`load_config()`)
   - initialize workspace (`init_workspace(paths)`)
   - run supervisor via `run_bot(config)` (transport-agnostic)
3. `run_bot()` acquires PID lock and starts `AgentSupervisor`

### Supervisor startup (`multiagent/supervisor.py`)

1. start `InterAgentBus`
2. start `InternalAgentAPI`
3. optional shared `TaskHub` (`tasks.enabled=true`)
4. create/start main `AgentStack`
5. wait for main readiness (`_main_ready`)
6. load/start sub-agents from `agents.json`
7. start `SharedKnowledgeSync`
8. start `agents.json` watcher
9. block on main completion and return its exit code

### Bot startup (Telegram: `messenger/telegram/startup.py`, Matrix: `messenger/matrix/startup.py`)

Telegram startup:

1. create orchestrator (`Orchestrator.create(...)`)
2. initialize chat tracker (`chat_activity.json`)
3. seed `TopicNameCache` from persisted sessions and wire topic name resolver into `SessionManager`
4. consume restart sentinel and optional upgrade sentinel
5. wire observers to message bus (`orch.wire_observers_to_bus(...)`)
6. register config hot-reload callback for auth/group updates
7. startup classification (`first_start`/`service_restart`/`system_reboot`) + startup notification policy
8. recovery planning (`inflight_turns.json` + recovered named sessions)
9. start update observer (upgradeable installs only, main agent only), sync Telegram commands, start restart watcher
10. run group audit immediately + start periodic 24h audit loop

Matrix startup follows a similar pattern (orchestrator creation, bus wiring, observer startup) but uses matrix-nio's `AsyncClient` sync loop instead of aiogram polling. `UpdateObserver` starts only for the main agent on both transports (sub-agents skip it).

### Orchestrator factory (`orchestrator/lifecycle.py`)

1. resolve paths and set `DUCTOR_HOME` for main agent
2. optional Docker setup + Docker-mode skill resync
3. inject runtime environment note into workspace rule files
4. instantiate `Orchestrator`
5. check provider auth and apply provider availability
6. initialize model cache observers (Gemini + Antigravity + Grok + Codex), only for provider CLIs found by startup auth detection
7. initialize task observers (`BackgroundObserver`, `CronObserver`, `WebhookObserver`)
8. start observers (`cron`, `heartbeat`, `webhook`, `cleanup`) + rule/skill watchers
9. optional API server startup
10. start config reloader

## Command Ownership and Routing

Bot-level handlers (`messenger/telegram/app.py`):

- `/start`, `/help`, `/info`, `/showfiles`, `/stop`, `/stop_all`, `/interrupt`, `/restart`, `/new`, `/reset`, `/session`, `/sessions`, `/tasks`, `/agent_commands`
- main-agent-only handlers: `/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`

Matrix command ownership (`messenger/matrix/bot.py`):

- direct transport commands: `!stop`, `!stop_all`, `!interrupt`, `!restart`, `!new`, `!reset`, `!help`, `!info`, `!session`, `!showfiles`, `!agent_commands`
- orchestrator-routed commands: `!status`, `!model`, `!memory`, `!cron`, `!diagnose`, `!upgrade`, `!sessions`, `!tasks`
- main-agent-only multi-agent commands: `!agents`, `!agent_start`, `!agent_stop`, `!agent_restart` (`/` prefix also supported)

Orchestrator command registry (`orchestrator/commands.py`):

- `/new`, `/reset`, `/status`, `/model`, `/memory`, `/cron`, `/diagnose`, `/upgrade`, `/sessions`, `/tasks`
- multi-agent commands are registered at runtime by supervisor hook

Abort behavior:

- `/stop` and `/stop_all` are handled before normal lock routing
- main-agent `/stop_all` uses supervisor callback to abort across all stacks

Quick-command bypass (`SequentialMiddleware`):

- `/status`, `/memory`, `/cron`, `/diagnose`, `/model`, `/showfiles`, `/sessions`, `/tasks`, `/where`, `/leave`

## Session and Topic Model

Sessions are keyed by `SessionKey(transport, chat_id, topic_id)`.

- Telegram forum topics are isolated from each other and from the base chat
- Matrix rooms use `transport="mx"` with deterministic int room mapping
- API sessions use `transport="api"` and optional `channel_id -> topic_id`
- `sessions.json` remains backward-compatible with legacy unprefixed keys
- topic names are cached from forum topic events and shown in `/status` and `/sessions`
- `/new @topicname` resets a specific topic session without switching to that topic

Provider isolation inside a session:

- each session has provider-local buckets (`provider_sessions`)
- switching provider/model preserves other provider buckets
- `/new` resets the configured default-provider bucket
- `/reset` resets the currently active provider bucket

Per-topic `/model` behavior:

- inside a topic, model/provider switch updates that topic session only
- global config (`config.json` / `agents.json`) is updated only outside topic scope

`/new` behavior:

- `/new` kills active work for the current chat/topic and resets the provider bucket tied to `config.model`
- it does **not** clear every provider bucket and does not target the last temporary `/model` switch automatically

## Flow Details

### Normal and streaming flows (`orchestrator/flows.py`)

1. resolve runtime target (provider/model)
2. resolve session by `SessionKey`
3. new session: append `MAINMEMORY.md` (+ agent roster context if available)
4. apply message hooks
5. build `AgentRequest` with `topic_id`
6. persist in-flight foreground turn (`InflightTracker.begin`)
7. execute CLI (`execute` or `execute_streaming`)
8. session recovery (single retry) on:
   - SIGKILL
   - invalid resumed session
9. on streaming compaction boundaries: mark session for post-turn memory flush/compaction
10. update session metrics and ID on success
11. after successful streaming turn: optional silent memory flush + compaction
12. clear inflight marker in `finally`

Gemini safeguard:

- if Gemini is in API-key mode and `gemini_api_key` is empty/null, flow returns warning text and skips CLI execution.

Successful empty-result safeguard:

- tool-only successful turns no longer disappear silently; `_finish_normal(...)` substitutes a neutral status line when the provider returns an empty success payload

### Heartbeat flow

- read-only active-session lookup (no create)
- skips when no session, provider mismatch, or cooldown not reached
- executes prompt with session resume
- suppresses pure ACK responses
- updates session only for non-ACK alerts

### Named sessions (`/session`)

- `BackgroundObserver` executes named session turns asynchronously
- follow-up support:
  - foreground: `@session-name <message>`
  - background: `/session @session-name <message>`
- `/sessions` interactive management via selector callbacks

### Delegated tasks (`TaskHub`)

- shared registry: `~/.ductor/tasks.json`
- folders: `~/.ductor/workspace/tasks/<task_id>/`
- endpoints via internal API (`/tasks/*`)
- topic-aware routing: task results/questions retain `thread_id` and are injected back into originating topic session
- task tools receive `DUCTOR_CHAT_ID` and optional `DUCTOR_TOPIC_ID`
- single-task permanent delete: `/tasks/delete` + `TaskRegistry.delete()`

## MessageBus and Delivery

`MessageBus` replaces fragmented delivery paths.

- `Envelope` captures origin, lock mode, injection requirements, delivery mode
- observers are wired in one call: `ObserverManager.wire_to_bus(...)`
- Telegram transport formatting is centralized in `messenger/telegram/transport.py`
- shared Telegram/message-bus `LockPool` prevents lock drift across middleware and background delivery
- `ApiServer` currently uses its own `LockPool`, so API locking is isolated from Telegram/message-bus locking
- Transport-aware delivery: each `Envelope` carries a `transport` field; UNICAST envelopes are routed only to the matching transport, with cascading fallback to other transports when the target is unavailable

## Callback Query Routing

Special callback namespaces:

- `mq:*` queue cancel
- `upg:*` upgrade
- `ms:*` model selector
- `crn:*` cron selector
- `nsc:*` session selector
- `tsc:*` task selector
- `ns:*` named-session follow-up
- `sf:*` / `sf!` file browser

Selector callbacks use transport-agnostic selector types (`Button`, `ButtonGrid`, `SelectorResponse`) from `orchestrator/selectors/models.py`.

## API Architecture

`ApiServer` (`api/server.py`) provides:

- websocket auth + E2E (`type=auth`, `token`, `e2e_pk`)
- optional auth-time session overrides:
  - `chat_id` (required > 0 when provided)
  - `channel_id` (maps to `SessionKey.topic_id`)
- encrypted message streaming events (`text_delta`, `tool_activity`, `system_status`, `result`)
- encrypted abort
- bearer-auth HTTP endpoints (`/files`, `/upload`)

## Restart and Shutdown

Restart triggers:

- `/restart` sentinel + exit code `42`
- external restart marker file
- main-agent restart propagates to process/service level

Shutdown (`orchestrator/lifecycle.shutdown`):

1. kill active CLI processes
2. stop API server
3. cleanup managed skill links
4. stop observers + config reloader + cache observers + watchers
5. optional Docker teardown

## Workspace Seeding Model

Source: `ductor_bot/_home_defaults/`.

Zone rules (`workspace/init.py`):

- Zone 2 overwrite:
  - `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`
  - tool scripts under `workspace/tools/{cron,webhook,agent,task}_tools/*.py`
- Zone 3 seed-once for other files
- `RULES*.md` templates are selected/deployed by `RulesSelector`

Rule sync:

- recursive mtime sync for `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`
- task-folder provider rules backfilled by `ensure_task_rule_files(...)`

## Multi-Agent Notes

- sub-agents are full stacks with own transport credentials/workspace/session files (each sub-agent can use a different transport)
- all stacks share one event loop, inter-agent bus, and optional shared task hub
- async inter-agent results are injected via bus envelopes
- provider switch during scoped `ia.<sender-slug>.t<topic>.x<hash>` conversations (legacy `ia-<sender>` without source context) auto-resets that named session and surfaces a provider-switch notice
- async agent-tool pipelines can route replies explicitly via `reply_to` and suppress recipient noise via `silent`
