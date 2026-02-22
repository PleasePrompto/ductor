# ductor Docs

ductor is a Telegram bot that forwards chat input to Claude Code CLI or OpenAI Codex CLI, streams replies back to Telegram, persists sessions in JSON, and runs cron/heartbeat/webhook automation plus daily file cleanup in-process.

## Onboarding (Read in This Order)

1. `docs/developer_quickstart.md` -- fastest path for junior contributors: run, debug, and understand hot paths.
2. `docs/modules/setup_wizard.md` -- CLI commands, onboarding wizard, auto-update system.
3. `docs/architecture.md` -- end-to-end runtime flow (startup, messages, callbacks, cron, heartbeat, webhooks, cleanup).
4. `docs/config.md` -- config schema, merge behavior, provider/model resolution.
5. `docs/modules/orchestrator.md` -- routing and flow control.
6. `docs/modules/bot.md` -- Telegram ingress, middleware, streaming UX, callbacks.
7. `docs/modules/cli.md` -- subprocess providers, stream event handling, fallback rules.
8. `docs/modules/workspace.md` -- `~/.ductor` layout, seeding, auth-based RULES deployment, rule-file sync.
9. Remaining module docs (`session`, `cron`, `heartbeat`, `webhook`, `cleanup`, `security`, `infra`, `logging`).

## System in 60 Seconds

- `ductor_bot/bot/`: Telegram handlers, auth/sequencing middleware, streaming editors, rich sender, file browser, response formatting.
- `ductor_bot/orchestrator/`: command dispatch, directives/hooks, normal and heartbeat flows, model selector.
- `ductor_bot/cli/`: Claude/Codex wrappers, provider-specific CLI parameter routing, Codex model cache, process registry, normalized stream events.
- `ductor_bot/session/`: per-chat session lifecycle in `sessions.json` with provider-isolated IDs/metrics (Claude and Codex keep separate session buckets).
- `ductor_bot/cron/`: in-process scheduler for `cron_jobs.json` with per-job execution overrides, quiet-hour gates, and dependency queue locking.
- `ductor_bot/heartbeat/`: periodic checks in active sessions.
- `ductor_bot/webhook/`: HTTP ingress (`/hooks/{hook_id}`) with per-hook auth (`bearer` or `hmac`), `wake`/`cron_task` modes, per-hook execution overrides, quiet-hour gates, and dependency queue locking.
- `ductor_bot/cleanup/`: daily retention cleanup for `telegram_files/` and `output_to_user/`.
- `ductor_bot/workspace/`: path resolution, home seeding from `ductor_bot/_home_defaults/`, auth-based RULES template deployment to `CLAUDE.md`/`AGENTS.md`, rule sync, cross-platform skill directory sync.
- `ductor_bot/infra/`: PID lock, restart sentinel, Docker helper, cross-platform service manager (systemd/launchd/Task Scheduler), auto-update observer (upgradeable installs), version check.
- `ductor_bot/cli/init_wizard.py`: interactive onboarding wizard, smart reset.
- `ductor_bot/log_context.py` + `ductor_bot/logging_config.py`: context-aware logging and log sinks.

Runtime behavior note:

- CLI errors do not auto-reset sessions. The session is preserved; users can retry with the same context or run `/new` explicitly.

## Documentation Index

- [Architecture](architecture.md)
- [Installation](installation.md)
- [Automation Quickstart](automation.md)
- [Developer Quickstart](developer_quickstart.md)
- [Configuration](config.md)
- Module docs:
  - [setup_wizard](modules/setup_wizard.md)
  - [bot](modules/bot.md)
  - [cli](modules/cli.md)
  - [orchestrator](modules/orchestrator.md)
  - [workspace](modules/workspace.md)
  - [skill_system](modules/skill_system.md)
  - [session](modules/session.md)
  - [cron](modules/cron.md)
  - [heartbeat](modules/heartbeat.md)
  - [webhook](modules/webhook.md)
  - [cleanup](modules/cleanup.md)
  - [security](modules/security.md)
  - [infra](modules/infra.md)
  - [logging](modules/logging.md)
