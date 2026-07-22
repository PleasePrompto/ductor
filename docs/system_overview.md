# System Overview

Fastest end-to-end mental model for ductor.

## 1) Runtime shape

One Python process hosts:

- main agent stack (always)
- optional sub-agent stacks from `~/.ductor/agents.json`
- shared `AgentSupervisor`
- shared `InterAgentBus`
- shared internal HTTP bridge (`InternalAgentAPI`, default port `8799`, configurable via `interagent_port`)
- shared `TaskHub` when `tasks.enabled=true`

Each agent stack contains:

- Transport bot (`TelegramBot` or `MatrixBot`, selected via
  `config.transport`; `MultiBotAdapter` enables parallel
  multi-transport execution when `config.transports` lists
  multiple entries)
- `Orchestrator` (routing + flows)
- `CLIService` (provider wrappers)
- provider subprocesses (`claude`, `codex`, `gemini`, `agy`, `grok`)

## 2) Primary message path

```text
Telegram:                                 Matrix:
  Telegram update                           Matrix sync event
  -> AuthMiddleware                         -> room/user allowlist check
  -> SequentialMiddleware                   -> MatrixBot handler
  -> bot handlers                           -> Orchestrator.handle_message(_streaming)
  -> Orchestrator.handle_message(_streaming)-> CLIService
  -> CLIService                             -> provider subprocess
  -> provider subprocess                    -> Matrix room message
  -> Telegram response (stream edits)
```

Notes:

- `/stop` and `/stop_all` are middleware/bot-level abort paths (not orchestrator command dispatch).
- `/new` resets the configured default-provider bucket for the active session key.
- `/reset` resets the currently active provider bucket for the active session key.
- Telegram groups: both `allowed_group_ids` and `allowed_user_ids` must allow the message.
- `group_mention_only` behavior differs by transport:
  - Telegram: mention/reply gating only (no auth bypass)
  - Matrix non-DM rooms: user allowlist check is bypassed; room allowlist + mention/reply are used as the gate

## 3) Session identity model

Session identity is transport-aware via `SessionKey(transport, chat_id, topic_id)`.

- Telegram normal chats: `transport="tg"`, `topic_id=None`
- Telegram forum topics: `transport="tg"`, `topic_id=message_thread_id`
- Matrix rooms: `transport="mx"`, `chat_id=<deterministic room-int>`
- API channel isolation: `transport="api"`, `topic_id=channel_id` (from auth payload)

Persistence key format in `sessions.json`:

- legacy flat: `"<chat_id>"` / `"<chat_id>:<topic_id>"` (still accepted on parse)
- current: `"<transport>:<chat_id>"` or `"<transport>:<chat_id>:<topic_id>"`

This keeps cross-transport and topic/channel conversations isolated while staying backward-compatible.

## 4) Background and delivery model

Observers run in-process (cron, webhook, heartbeat, cleanup, background sessions, model caches, config watcher, rule/skill sync).

All observer/task/inter-agent results now flow through `bus/`:

- wrap to `Envelope` (`bus/adapters.py`)
- route via `MessageBus`
- optional lock + optional injection into active session
- deliver through registered transport (Telegram or Matrix)

Streaming nuance:

- when the provider stream emits `CompactBoundaryEvent`, the orchestrator can mark the session for post-turn memory maintenance
- after a successful streaming turn, `MemoryFlusher` may run a silent flush and then an LLM-driven compaction pass against `MAINMEMORY.md`

Telegram ingress and `MessageBus` share one `LockPool`. `ApiServer` currently uses its own `LockPool`, so API locking is separate from the Telegram/message-bus lock domain.

## 5) Optional direct API path

When `api.enabled=true` and PyNaCl is installed:

```text
/ws
  -> plaintext auth frame (token + e2e_pk + optional chat_id/channel_id)
  -> auth_ok
  -> encrypted frames (NaCl Box)
  -> orchestrator streaming callbacks
```

HTTP endpoints:

- `GET /health`
- `GET /files?path=...` (Bearer token + root checks)
- `POST /upload` (Bearer token, multipart)

## 6) Internal localhost bridge

`InternalAgentAPI` endpoints for CLI tool scripts:

- `/interagent/send`
- `/interagent/send_async`
- `/interagent/agents`
- `/interagent/health`
- `/tasks/create`
- `/tasks/resume`
- `/tasks/ask_parent`
- `/tasks/list`
- `/tasks/cancel`
- `/tasks/delete`

Ownership checks are enforced for resume/cancel/delete when `from=<agent>` is supplied.

## 7) Key runtime files (`~/.ductor`)

- `config/config.json`
- `sessions.json`
- `named_sessions.json`
- `tasks.json`
- `chat_activity.json`
- `cron_jobs.json`
- `webhooks.json`
- `agents.json`
- `startup_state.json`
- `inflight_turns.json`
- `SHAREDMEMORY.md`
- `logs/agent.log`
- `workspace/` (rules, tools, files, tasks, cron_tasks, skills)

Sub-agent home: `~/.ductor/agents/<name>/` with its own config/workspace/session files.

## 8) Where to read code first

1. `ductor_bot/__main__.py` (entrypoint + config/load/run)
2. `ductor_bot/cli_commands/` (actual CLI subcommand logic)
3. `ductor_bot/multiagent/supervisor.py` (always-on runtime wrapper)
4. `ductor_bot/messenger/telegram/app.py` + `messenger/telegram/startup.py` (Telegram), `ductor_bot/messenger/matrix/bot.py` (Matrix)
5. `ductor_bot/orchestrator/core.py` + `orchestrator/lifecycle.py`
6. `ductor_bot/bus/*` (unified delivery/injection)
7. `ductor_bot/tasks/hub.py` + `tasks/registry.py`
8. `ductor_bot/cli/service.py` and provider wrappers

## 9) Command surface (high level)

Chat commands (Telegram and Matrix):

- `/new`, `/reset`, `/stop`, `/stop_all`, `/interrupt`, `/model`, `/status`, `/memory`, `/session`, `/sessions`, `/tasks`, `/cron`, `/showfiles`, `/info`, `/help`, `/diagnose`, `/upgrade`
- Telegram-only utility commands: `/where`, `/leave` (work but are not in command popup)
- Matrix uses `!` prefix by default (e.g. `!help`, `!status`); `/` also works but may conflict with Element's built-in commands

Main-agent only (chat commands):

- Telegram: `/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`
- Matrix: `!agents`, `!agent_start`, `!agent_stop`, `!agent_restart` (`/` prefix also supported)

Available on all agents:

- Telegram: `/agent_commands`
- Matrix: `!agent_commands` (`/` prefix also supported)

CLI:

- `ductor`
- `ductor status|stop|restart|upgrade|uninstall|onboarding|reset|help`
- `ductor service ...`
- `ductor docker ...` (includes `extras`, `extras-add`, `extras-remove` for optional AI/ML packages)
- `ductor api ...`
- `ductor agents ...` (`add` currently scaffolds Telegram sub-agents; Matrix sub-agents are added via `agents.json` or tool scripts)
- `ductor install <extra>` (`matrix`, `api`)

## 10) Service runtime model

Background service management is platform-dispatched by `infra/service.py`:

- Linux -> systemd user service
- macOS -> launchd Launch Agent
- Windows -> Task Scheduler task

Operational notes:

- onboarding offers service install on every platform where a backend is available
- `stop_bot()` stops the installed service first so it does not immediately respawn the process
- `ductor service logs` follows `journalctl` on Linux and tails file logs on macOS/Windows

Deep dive: [service_management](modules/service_management.md)
