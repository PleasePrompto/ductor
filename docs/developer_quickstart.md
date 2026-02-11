# Developer Quickstart

This is the fastest onboarding path for contributors and junior devs.

## 1) Local Setup (5 minutes)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Optional but recommended if you test full runtime behavior:

- Install and authenticate at least one provider CLI (`claude` or `codex`).
- Create a Telegram bot token and user ID.

## 2) Run the Bot

```bash
ductor
```

First run starts onboarding automatically and writes config to:

- `~/.ductor/config/config.json`

Key runtime paths:

- `~/.ductor/sessions.json`
- `~/.ductor/cron_jobs.json`
- `~/.ductor/webhooks.json`
- `~/.ductor/workspace/`
- `~/.ductor/logs/agent.log`

## 3) Quality Gates

```bash
pytest
ruff format .
ruff check .
mypy ductor_bot
```

Expected standard: zero warnings, zero errors.

## 4) Core Mental Model

Runtime flow:

```text
Telegram update
  -> bot layer (handlers + middleware)
  -> orchestrator (routing + flows)
  -> CLI service (Claude/Codex subprocess)
  -> streamed/non-streamed response back to Telegram
```

Background observers run in the same process:

- cron
- heartbeat
- webhook
- cleanup
- update checker
- rule sync
- skill sync

## 5) Where to Start Reading Code

Entry points:

- `ductor_bot/__main__.py` (CLI dispatch + process start)
- `ductor_bot/bot/app.py` (Telegram handlers + callback routing)
- `ductor_bot/orchestrator/core.py` (main routing + observer wiring)

Hot modules by responsibility:

- Chat UX and queueing: `ductor_bot/bot/middleware.py`
- Message flows: `ductor_bot/orchestrator/flows.py`
- Command handlers: `ductor_bot/orchestrator/commands.py`
- CLI wrappers: `ductor_bot/cli/service.py`, `ductor_bot/cli/claude_provider.py`, `ductor_bot/cli/codex_provider.py`
- Workspace/rules/skill sync: `ductor_bot/workspace/init.py`, `ductor_bot/workspace/rules_selector.py`, `ductor_bot/workspace/skill_sync.py`

## 6) Most Common Debug Paths

If a message is not handled correctly:

1. Check `ductor_bot/bot/middleware.py` (auth, quick-command bypass, lock/queue).
2. Check `ductor_bot/bot/app.py` route (`_on_message`, `_on_command`, `_on_callback_query`).
3. Check `ductor_bot/orchestrator/core.py::_route_message`.
4. Check provider execution in `ductor_bot/cli/service.py`.

If automation is not firing:

1. Cron: `ductor_bot/cron/observer.py` + `~/.ductor/cron_jobs.json`
2. Webhooks: `ductor_bot/webhook/server.py` + `ductor_bot/webhook/observer.py` + `~/.ductor/webhooks.json`
3. Heartbeat: `ductor_bot/heartbeat/observer.py`
4. Quiet-hour checks: `ductor_bot/utils/quiet_hours.py`
5. Dependency locking: `ductor_bot/cron/dependency_queue.py`

If rules/skills look wrong:

1. `ductor_bot/workspace/init.py`
2. `ductor_bot/workspace/rules_selector.py`
3. `ductor_bot/workspace/skill_sync.py`

## 7) Important Behavior Details

- `/stop` abort handling and queue draining are middleware-level behavior (`SequentialMiddleware`) before normal message routing.
- `/new` from bot handlers resets session state; `cmd_reset` in orchestrator also kills active processes (used only when routing passes through orchestrator registry).
- Cron jobs and webhook `cron_task` runs can be gated by quiet hours and serialized via shared dependency keys.
- Zone 2 sync in workspace init always overwrites:
  - `CLAUDE.md`, `AGENTS.md`
  - framework tool scripts: `workspace/tools/cron_tools/*.py`, `workspace/tools/webhook_tools/*.py`

For deeper module-level docs, continue with `docs/architecture.md` and `docs/modules/*.md`.
