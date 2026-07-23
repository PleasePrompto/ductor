This file gives coding agents a current map of the repository.

## Project Overview

ductor is a multi-transport chat orchestrator for the official provider CLIs (`claude`, `codex`, `gemini`, `agy`, `grok`).
It runs Telegram, Matrix and/or Slack, can expose an optional direct WebSocket API, keeps state under `~/.ductor`, and supervises the main agent plus optional sub-agents in one asyncio process.

Stack:

- Python 3.11+
- aiogram 3.x (Telegram)
- matrix-nio (Matrix, optional extra)
- slack-bolt (Slack, optional extra)
- aiohttp (webhook server, internal API, optional direct API)
- Pydantic 2.x
- asyncio

## Development Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run
ductor
ductor -v

# Tests
pytest
pytest -k "pattern"

# Quality
ruff format .
ruff check .
mypy ductor_bot
```

## Runtime Flow

```text
Telegram:
  Update -> AuthMiddleware -> SequentialMiddleware -> TelegramBot
  -> Orchestrator -> CLIService -> provider subprocess -> Telegram delivery

Matrix:
  sync event -> MatrixBot auth/room checks -> Orchestrator
  -> CLIService -> provider subprocess -> Matrix delivery

API (optional):
  /ws auth (token + e2e_pk + optional chat_id/channel_id)
  -> encrypted frames -> Orchestrator streaming -> encrypted result events
```

Background and async delivery:

```text
Observer / TaskHub / InterAgentBus callback
  -> bus.adapters -> Envelope -> MessageBus
  -> optional shared lock + optional session injection
  -> registered transport adapters (TelegramTransport / MatrixTransport)
```

## Module Map

| Module | Purpose |
|---|---|
| `cli_commands/` | CLI command implementations (`service`, `docker`, `api`, `agents`, lifecycle, install, status) |
| `messenger/` | transport protocol, capabilities, notifications, registry, multi-transport adapter |
| `messenger/telegram/` | Telegram transport: middleware, handlers, startup, callback routing, file/media UX |
| `messenger/matrix/` | Matrix transport: sync loop, auth, segment streaming, reaction buttons, media |
| `messenger/slack/` | Slack transport: Socket Mode bot, native streaming, ID mapping, sender/media |
| `orchestrator/` | command routing, directives/hooks, flows, provider/session/task wiring, lifecycle split |
| `bus/` | unified `Envelope`, `MessageBus`, shared `LockPool`, delivery adapters |
| `cli/` | provider wrappers, stream parsing, auth detection, model caches, process registry |
| `session/` | `SessionKey(transport, chat_id, topic_id)`, provider-isolated session buckets, named sessions |
| `tasks/` | delegated background task runtime (`TaskHub`) and persistent registry |
| `background/` | named background session execution for `/session` |
| `multiagent/` | supervisor, inter-agent bus, internal localhost API bridge, shared knowledge sync |
| `api/` | optional direct WebSocket API and authenticated file endpoints |
| `cron/`, `webhook/`, `heartbeat/`, `cleanup/` | in-process automation observers |
| `workspace/` | `~/.ductor` path model, seeding, rule deployment/sync, skill sync |
| `infra/` | PID lock, service backends, Docker manager, restart/update/recovery helpers |
| `files/` | shared file/path safety, MIME detection, image processing (`image_processor.py`: resize/convert incoming images) |
| `security/`, `text/` | prompt safety, formatting helpers |

## Key Runtime Patterns

- `DuctorPaths` in `workspace/paths.py` is the single source of truth for runtime paths.
- Session identity is `SessionKey(transport, chat_id, topic_id)` across Telegram chats/topics, Matrix rooms (mapped int), and API channel isolation.
- `/new` resets the configured default-provider bucket for the active session key.
- `/reset` resets the currently active provider bucket for the active session key.
- `MessageBus` is the single async delivery path for observers, task callbacks, webhook wake results, and async inter-agent responses. Delivery is transport-aware: UNICAST envelopes route to the matching transport only, with cascading fallback when the target transport is unavailable.
- Telegram ingress and `MessageBus` share one `LockPool`; `ApiServer` currently uses its own lock pool.
- Workspace init is zone-based:
  - Zone 2 overwrite: `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, framework-managed tool scripts
  - Zone 3 seed-once: user-owned files
- Rule sync is mtime-based for sibling `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`; cron task folders additionally get missing rule backfill. `AGENTS.md` serves Codex **and** Grok — like Codex, Grok loads project rules from `AGENTS.md` (`RulesSelector` deploys it when either is authenticated).
- Skill sync spans `~/.ductor/workspace/skills`, `~/.claude/skills`, `~/.codex/skills`, `~/.gemini/skills`, `~/.grok/skills`:
  - normal mode: links/junctions
  - Docker mode: managed copies (`.ductor_managed`)
- `ductor agents add` is a Telegram-focused scaffold; Matrix sub-agents are supported through `agents.json` or the bundled agent tool scripts.

## Background Systems

All run as in-process asyncio tasks:

- `BackgroundObserver` (named sessions)
- `CronObserver` (results route UNICAST when job has `chat_id`, BROADCAST otherwise)
- `WebhookObserver`
- `HeartbeatObserver` (supports `group_targets` with per-target overrides)
- `CleanupObserver`
- `CodexCacheObserver`
- `GeminiCacheObserver`
- `AntigravityCacheObserver`
- `GrokCacheObserver`
- config reloader
- rule sync watcher
- skill sync watcher
- update observer (upgradeable installs)

The four model-cache observers start only for provider CLIs found by startup auth detection (`installed_providers`), not by a plain PATH lookup.

## Service Backends

Platform dispatch lives in `infra/service.py`:

- Linux: systemd user service (`infra/service_linux.py`)
- macOS: launchd Launch Agent (`infra/service_macos.py`)
- Windows: Task Scheduler (`infra/service_windows.py`)

Operational notes:

- onboarding offers service install when a backend is available
- `stop_bot()` stops the installed service first so it does not immediately respawn the process
- `ductor service logs` behavior:
  - Linux: `journalctl --user -u ductor -f`
  - macOS/Windows: recent lines from `~/.ductor/logs/agent.log` (fallback newest `*.log`)

## CLI Surface

Core:

- `ductor`, `ductor onboarding`, `ductor reset`
- `ductor status`, `ductor stop`, `ductor restart`, `ductor upgrade`, `ductor uninstall`

Groups:

- `ductor service <install|status|start|stop|logs|uninstall>`
- `ductor docker <rebuild|enable|disable|mount|unmount|mounts|extras|extras-add|extras-remove>`
- `ductor api <enable|disable>`
- `ductor agents <list|add|remove>`
- `ductor install <matrix|slack|api>`

Nuances:

- `ductor agents add` interactively scaffolds Telegram sub-agents only
- Matrix sub-agents are still first-class at runtime; define them in `agents.json` or via the bundled agent tools

## Key Data Files (`~/.ductor`)

- `config/config.json`
- `.env`
- `sessions.json`
- `named_sessions.json`
- `tasks.json`
- `cron_jobs.json`
- `webhooks.json`
- `agents.json`
- `startup_state.json`
- `inflight_turns.json`
- `chat_activity.json`
- `SHAREDMEMORY.md`
- `logs/agent.log`
- `workspace/`

## Conventions

- `asyncio_mode = "auto"` in tests
- line length 100
- mypy strict mode
- ruff strict lint profile
- config deep-merge adds new defaults without dropping user keys
- supervisor restart code is `42`
