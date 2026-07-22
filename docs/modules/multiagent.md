# multiagent/

Multi-agent runtime: run multiple independent ductor agents in one process.

## Files

- `multiagent/supervisor.py`: `AgentSupervisor` lifecycle, health, crash recovery, agents watcher
- `multiagent/stack.py`: `AgentStack` container (config + bot + orchestrator)
- `multiagent/bus.py`: `InterAgentBus` (sync + async inter-agent messaging)
- `multiagent/internal_api.py`: localhost HTTP bridge for tool scripts (`/interagent/*`, `/tasks/*`)
- `multiagent/models.py`: `SubAgentConfig`, merge helpers
- `multiagent/registry.py`: `agents.json` read/write
- `multiagent/health.py`: per-agent health model
- `multiagent/shared_knowledge.py`: shared knowledge sync (`SHAREDMEMORY.md`)
- `multiagent/commands.py`: Telegram commands (`/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`)

## Runtime model

```text
AgentSupervisor
  +-- AgentStack "main"
  +-- AgentStack "sub-*" (0..n)
  +-- InterAgentBus
  +-- InternalAgentAPI (localhost bridge)
  +-- optional TaskHub (shared)
  +-- SharedKnowledgeSync
  +-- FileWatcher(agents.json)
```

Each stack is isolated (token/workspace/sessions), but shares process/event-loop infrastructure.

## Startup sequence (`AgentSupervisor.start`)

1. start inter-agent bus
2. start internal API
3. optional shared task hub
4. create/start main stack
5. wait for main readiness
6. start sub-agents from `agents.json`
7. start shared knowledge sync
8. start `agents.json` watcher

## Dynamic agent changes

Watcher polls `agents.json` every 5s.

- added entry -> start sub-agent
- removed entry -> stop sub-agent
- restart triggers for running sub-agents:
  - `transport` changed
  - Telegram identity changed (`telegram_token`)
  - Matrix identity changed (`matrix.homeserver` or `matrix.user_id`)
- other field changes do not auto-restart running agent

## Crash/restart policy

Per agent `_supervised_run` behavior:

- clean exit -> stop
- exit code `42`:
  - main -> propagate full process/service restart
  - sub-agent -> in-process hot-reload
- crash:
  - sub-agent -> exponential backoff retries (5s, 10s, 20s, 40s, 80s), give up after `_MAX_RESTART_RETRIES` (5) and mark `crashed` (main agent is notified)
  - main agent -> **fatal** errors terminate the supervisor; **transient** network errors (`ConnectionError`, `TimeoutError`, `TelegramNetworkError`, `TelegramServerError`) retry **indefinitely** with capped backoff (5, 10, 20, 40, 60, 60, … s), so an early-startup network blip cannot take down the whole service. Fatal transport errors such as `TelegramUnauthorizedError` (bad token) and `TelegramConflictError` (double polling) still terminate.

## Sub-agent config (`agents.json`)

Minimal entry:

```json
{
  "name": "coder",
  "telegram_token": "123456:ABC...",
  "allowed_user_ids": [12345678],
  "allowed_group_ids": [],
  "provider": "codex",
  "model": "o3"
}
```

Merge behavior:

- base: main `AgentConfig`
- override: non-null `SubAgentConfig` fields
- always forced:
  - `ductor_home=~/.ductor/agents/<name>/`
  - sub-agent `telegram_token`, `allowed_user_ids`, `allowed_group_ids`
  - `api.enabled=false` unless explicitly provided for sub-agent

## Shared vs isolated

Isolated per sub-agent:

- transport credentials and auth (Telegram token or Matrix account)
- workspace and files under `~/.ductor/agents/<name>/`
- `sessions.json`, `named_sessions.json`, cron/webhook state

Shared across process:

- `InterAgentBus`
- `InternalAgentAPI`
- optional shared `TaskHub`
- central log file (`~/.ductor/logs/agent.log`)
- shared knowledge source (`~/.ductor/SHAREDMEMORY.md`)

## Inter-agent communication

### In-memory bus

- sync: waits for target response (`send`)
- async: returns task ID immediately (`send_async`)

Recipient processing uses deterministic scoped session `ia.<sender-slug>.t<topic>.x<hash>`, or legacy `ia-<sender>` without source context.

Async send metadata currently supports:

- `new_session`
- `summary`
- `chat_id` / `topic_id` for result routing back to the correct Telegram topic
- `reply_to` to deliver the final async result to a specific agent instead of the sender field
- `silent` to suppress the recipient-side "async task received" notification

Provider-switch safeguard:

- if the recipient provider changed since the prior scoped or legacy inter-agent session, the old session is ended and recreated
- provider-switch notice is surfaced back to sender side
- when `reply_to` is set, async result handler lookup uses that agent name instead of `sender`
- recipient notifications are skipped when `silent=true` or `reply_to` is set

### Local HTTP bridge for tool scripts

`InternalAgentAPI` runs on `config.interagent_port` (default `8799`):

- host mode: `127.0.0.1:<port>`
- Docker mode: `0.0.0.0:<port>`

Inter-agent endpoints:

- `POST /interagent/send`
- `POST /interagent/send_async`
- `GET /interagent/agents`
- `GET /interagent/health`

`POST /interagent/send_async` accepts the same routing fields as `AsyncSendOptions`, including `reply_to` and `silent`.

Task endpoints (shared hub):

- `POST /tasks/create`
- `POST /tasks/resume`
- `POST /tasks/ask_parent`
- `GET /tasks/list`
- `POST /tasks/cancel`
- `POST /tasks/delete`

Ownership checks apply for resume/cancel/delete when `from=<agent>` is present.

`POST /tasks/create` also accepts `priority=interactive|background|batch`.

## TaskHub integration

When enabled, supervisor wires each stack into shared `TaskHub`:

- per-agent CLI service
- per-agent paths (`tasks_dir`)
- task result callback
- task question callback
- agent primary chat ID mapping

This enables task submission from any agent while preserving owner routing.

Priority behavior is shared across agents:

- `interactive` bypasses the per-chat concurrency cap
- `background` is the default
- `batch` keeps the same cap semantics as `background` but documents low-urgency work explicitly

## Shared knowledge sync

`SharedKnowledgeSync` watches `~/.ductor/SHAREDMEMORY.md` and mirrors content into each agent's `MAINMEMORY.md` block.

Legacy HTML marker format is migrated to current block markers when rewritten.

## Chat and CLI commands

Main-agent chat commands:

- Telegram: `/agents`, `/agent_start <name>`, `/agent_stop <name>`, `/agent_restart <name>`
- Matrix: `!agents`, `!agent_start <name>`, `!agent_stop <name>`, `!agent_restart <name>` (`/` prefix also works)

CLI:

- `ductor agents`
- `ductor agents list`
- `ductor agents add <name>`
- `ductor agents remove <name>`

`ductor agents list` fetches live health from internal API when main bot is running.

Important CLI nuance:

- `ductor agents add <name>` currently prompts for Telegram token/user/group data only.
- Matrix sub-agents are supported by the runtime and merge logic, but are created via manual `agents.json` editing or the bundled `create_agent.py --transport matrix` tool script.
