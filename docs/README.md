# ductor Docs

ductor routes chat input to official provider CLIs (`claude`, `codex`, `gemini`, `agy`, `grok`), streams responses back via Telegram or Matrix, persists session state, and runs cron/heartbeat/webhook/cleanup automation in-process. It also supports a direct WebSocket API transport with authenticated file upload/download.

## Onboarding (Read in This Order)

1. `docs/system_overview.md` -- fastest end-to-end mental model.
2. `docs/developer_quickstart.md` -- shortest path for contributors/junior devs.
3. `docs/modules/setup_wizard.md` -- CLI commands, onboarding, restart/upgrade lifecycle.
4. `docs/modules/service_management.md` -- systemd/launchd/Task Scheduler backends and operational behavior.
5. `docs/architecture.md` -- startup, routing, streaming, callbacks, observers.
6. `docs/config.md` -- config schema, merge behavior, hot-reload boundaries.
7. `docs/modules/config_reload.md` -- runtime config reload details.
8. `docs/modules/orchestrator.md` -- routing core, flows, selectors, lifecycle split.
9. `docs/modules/bot.md` -- Telegram transport (messenger/telegram/), middleware, topic routing.
10. `docs/modules/bus.md` -- unified Envelope/MessageBus delivery architecture.
11. `docs/modules/session.md` -- transport-aware `SessionKey(transport, chat_id, topic_id)` isolation model.
12. `docs/modules/tasks.md` -- delegated task system (`TaskHub`) and `/tasks/*` API.
13. `docs/modules/api.md` -- direct WebSocket ingress and HTTP file endpoints.
14. `docs/modules/cli.md` -- provider wrappers, stream parsing, process control.
15. `docs/modules/cli_commands.md` -- CLI command split from `__main__.py`.
16. `docs/modules/workspace.md` -- `~/.ductor` seeding, rules sync, skill sync.
17. `docs/modules/multiagent.md` -- supervisor, bus bridge, sub-agent runtime.
18. Remaining module docs (`background`, `cron`, `webhook`, `heartbeat`, `cleanup`, `infra`, `supervisor`, `security`, `logging`, `files`, `text`, `skill_system`).

## System in 60 Seconds

- `ductor_bot/__main__.py`: thin CLI entrypoint (dispatch) + config loading.
- `ductor_bot/cli_commands/`: concrete CLI subcommand implementations (`agents`, `docker`, `service`, `api`, `install`, lifecycle/status helpers).
- `ductor_bot/messenger/`: transport-agnostic protocols, capabilities, notifications, registry.
- `ductor_bot/messenger/telegram/`: aiogram handlers, auth/sequencing middleware, streaming dispatch, callback routing, group audit/chat tracking.
- `ductor_bot/messenger/matrix/`: matrix-nio handlers, segment streaming, reaction buttons, formatting.
- `ductor_bot/orchestrator/`: command registry, directives/hooks, normal + streaming + heartbeat flows, provider/session/task wiring.
- `ductor_bot/bus/`: central `MessageBus` + `Envelope` + `LockPool`.
- `ductor_bot/session/`: provider-isolated session state keyed by `SessionKey(transport, chat_id, topic_id)` plus named-session registry.
- `ductor_bot/tasks/`: shared background task delegation (`TaskHub`) and persistent task registry.
- `ductor_bot/api/`: WebSocket ingress (`/ws`) and HTTP file endpoints (`/files`, `/upload`).
- `ductor_bot/cli/`: Claude/Codex/Gemini/Antigravity wrappers, stream-event normalization, auth checks, model caches, process registry.
- `ductor_bot/cron/`, `webhook/`, `heartbeat/`, `cleanup/`: in-process automation observers.
- `ductor_bot/workspace/`: path source-of-truth, home defaults sync, rules deployment/sync, skill sync.
- `ductor_bot/multiagent/`: supervisor, inter-agent bus, internal localhost API bridge, shared-knowledge sync.
- `ductor_bot/infra/`: PID lock, restart/update state, Docker manager, service backends, observer/task utilities.
- `ductor_bot/infra/service_*.py`: platform-specific service installation, control, and log access.

Runtime behavior notes:

- `/new` is a factory reset for the active `SessionKey`: it clears the configured default-provider bucket for that chat/topic and leaves other provider buckets intact.
- `/reset` clears the currently active provider bucket for that chat/topic.
- Forum topics are isolated: each topic has its own transport-aware `SessionKey(...)` state.
- Normal CLI errors do not auto-reset sessions; context is preserved unless explicit reset/recovery path applies.
- Startup can recover interrupted foreground turns and safely resume eligible named sessions.

## Documentation Index

- [Architecture](architecture.md)
- [System Overview](system_overview.md)
- [Installation](installation.md)
- [Matrix Setup](matrix-setup.md)
- [Slack Setup](slack-setup.md)
- [Automation Quickstart](automation.md)
- [Developer Quickstart](developer_quickstart.md)
- [Configuration](config.md)
- Module docs:
  - [setup_wizard](modules/setup_wizard.md)
  - [service_management](modules/service_management.md)
  - [cli_commands](modules/cli_commands.md)
  - [config_reload](modules/config_reload.md)
  - [messenger](modules/messenger.md)
  - [messenger/telegram](modules/bot.md)
  - [messenger/matrix](modules/matrix.md)
  - [bus](modules/bus.md)
  - [background](modules/background.md)
  - [session](modules/session.md)
  - [tasks](modules/tasks.md)
  - [api](modules/api.md)
  - [files](modules/files.md)
  - [text](modules/text.md)
  - [cli](modules/cli.md)
  - [orchestrator](modules/orchestrator.md)
  - [workspace](modules/workspace.md)
  - [skill_system](modules/skill_system.md)
  - [cron](modules/cron.md)
  - [webhook](modules/webhook.md)
  - [heartbeat](modules/heartbeat.md)
  - [cleanup](modules/cleanup.md)
  - [infra](modules/infra.md)
  - [supervisor](modules/supervisor.md)
  - [multiagent](modules/multiagent.md)
  - [security](modules/security.md)
  - [logging](modules/logging.md)
