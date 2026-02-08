# ductor Docs

ductor is a Telegram bot that forwards chat input to Claude Code CLI or OpenAI Codex CLI, streams replies back to Telegram, persists sessions in JSON, and runs cron/heartbeat/webhook automation in-process.

## Onboarding (Read in This Order)

1. `docs/modules/setup_wizard.md` -- CLI commands, onboarding wizard, auto-update system.
2. `docs/architecture.md` -- end-to-end runtime flow (startup, messages, callbacks, cron, heartbeat, webhooks).
3. `docs/config.md` -- config schema, merge behavior, provider/model resolution.
4. `docs/modules/orchestrator.md` -- routing and flow control.
5. `docs/modules/bot.md` -- Telegram ingress, middleware, streaming UX, callbacks.
6. `docs/modules/cli.md` -- subprocess providers, stream event handling, fallback rules.
7. `docs/modules/workspace.md` -- `~/.ductor` layout, seeding, rule-file sync.
8. Remaining module docs (`session`, `cron`, `heartbeat`, `webhook`, `security`, `infra`, `logging`).

## System in 60 Seconds

- `ductor_bot/bot/`: Telegram handlers, auth/sequencing middleware, streaming editors, rich sender.
- `ductor_bot/orchestrator/`: command dispatch, directives/hooks, normal and heartbeat flows, model selector.
- `ductor_bot/cli/`: Claude/Codex wrappers, process registry, normalized stream events.
- `ductor_bot/session/`: per-chat session lifecycle in `sessions.json`.
- `ductor_bot/cron/`: in-process scheduler for `cron_jobs.json`.
- `ductor_bot/heartbeat/`: periodic checks in active sessions.
- `ductor_bot/webhook/`: HTTP ingress (`/hooks/{hook_id}`) with per-hook auth (`bearer` or `hmac`) and `wake`/`cron_task` modes.
- `ductor_bot/workspace/`: path resolution, home seeding from `ductor_bot/_home_defaults/`, `CLAUDE.md`/`AGENTS.md` sync.
- `ductor_bot/infra/`: PID lock, restart sentinel, Docker helper, auto-update observer (upgradeable installs), version check.
- `ductor_bot/cli/init_wizard.py`: interactive onboarding wizard, smart reset.
- `ductor_bot/log_context.py` + `ductor_bot/logging_config.py`: context-aware logging and log sinks.

## Documentation Index

- [Architecture](architecture.md)
- [Configuration](config.md)
- Module docs:
  - [setup_wizard](modules/setup_wizard.md)
  - [bot](modules/bot.md)
  - [cli](modules/cli.md)
  - [orchestrator](modules/orchestrator.md)
  - [workspace](modules/workspace.md)
  - [session](modules/session.md)
  - [cron](modules/cron.md)
  - [heartbeat](modules/heartbeat.md)
  - [webhook](modules/webhook.md)
  - [security](modules/security.md)
  - [infra](modules/infra.md)
  - [logging](modules/logging.md)
