# Developer Quickstart

Fast onboarding path for contributors and junior devs.

## 1) Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Optional for full runtime validation:

- install/auth at least one provider CLI (`claude`, `codex`, `gemini`, `agy`, `grok`)
- set up a messaging transport:
  - **Telegram**: bot token from @BotFather + user ID (`allowed_user_ids`)
  - **Matrix**: account on any homeserver (homeserver URL, user ID, password, `allowed_users`)
- for Telegram group support, also set `allowed_group_ids`

## 2) Run the bot

```bash
ductor
```

First run starts onboarding and writes config to `~/.ductor/config/config.json`.

Primary runtime files/directories:

- `~/.ductor/sessions.json`
- `~/.ductor/named_sessions.json`
- `~/.ductor/tasks.json`
- `~/.ductor/chat_activity.json`
- `~/.ductor/cron_jobs.json`
- `~/.ductor/webhooks.json`
- `~/.ductor/startup_state.json`
- `~/.ductor/inflight_turns.json`
- `~/.ductor/SHAREDMEMORY.md`
- `~/.ductor/agents.json`
- `~/.ductor/agents/`
- `~/.ductor/workspace/`
- `~/.ductor/logs/agent.log`

## 3) Quality gates

```bash
pytest
ruff format .
ruff check .
mypy ductor_bot
```

Expected: zero warnings, zero errors.

## 4) Core mental model

```text
Telegram / Matrix / API input
  -> ingress layer (TelegramBot / MatrixBot / ApiServer)
  -> orchestrator flow
  -> provider CLI subprocess
  -> response delivery (transport-specific)

background/async results
  -> Envelope adapters
  -> MessageBus
  -> optional session injection
  -> transport delivery (Telegram or Matrix)
```

## 5) Read order in code

Entry + command layer:

- `ductor_bot/__main__.py`
- `ductor_bot/cli_commands/`

Runtime hot path:

- `ductor_bot/multiagent/supervisor.py`
- `ductor_bot/messenger/telegram/app.py`
- `ductor_bot/messenger/telegram/startup.py`
- `ductor_bot/orchestrator/core.py`
- `ductor_bot/orchestrator/lifecycle.py`
- `ductor_bot/orchestrator/flows.py`

Delivery/task/session core:

- `ductor_bot/bus/`
- `ductor_bot/session/manager.py`
- `ductor_bot/tasks/hub.py`
- `ductor_bot/tasks/registry.py`

Provider/API/workspace core:

- `ductor_bot/cli/service.py` + provider wrappers
- `ductor_bot/api/server.py`
- `ductor_bot/workspace/init.py`
- `ductor_bot/workspace/rules_selector.py`
- `ductor_bot/workspace/skill_sync.py`

## 6) Common debug paths

If command behavior is wrong:

1. `ductor_bot/__main__.py`
2. `ductor_bot/cli_commands/*`

If Telegram routing is wrong:

1. `ductor_bot/messenger/telegram/middleware.py`
2. `ductor_bot/messenger/telegram/app.py`
3. `ductor_bot/orchestrator/commands.py`
4. `ductor_bot/orchestrator/flows.py`

If Matrix routing is wrong:

1. `ductor_bot/messenger/matrix/bot.py`
2. `ductor_bot/messenger/matrix/transport.py`
3. `ductor_bot/orchestrator/flows.py`

If background results look wrong:

1. `ductor_bot/bus/adapters.py`
2. `ductor_bot/bus/bus.py`
3. `ductor_bot/messenger/telegram/transport.py` (or `ductor_bot/messenger/matrix/transport.py`)

If tasks are wrong:

1. `ductor_bot/tasks/hub.py`
2. `ductor_bot/tasks/registry.py`
3. `ductor_bot/multiagent/internal_api.py`
4. `ductor_bot/_home_defaults/workspace/tools/task_tools/*.py`

If API is wrong:

1. `ductor_bot/api/server.py`
2. `ductor_bot/orchestrator/lifecycle.py` (API startup wiring)
3. `ductor_bot/files/*` (allowed roots, MIME, prompt building)

## 7) Behavior details to remember

- `/stop` and `/stop_all` are pre-routing abort paths in middleware/bot.
- `/new` resets the configured default-provider bucket for the active `SessionKey`.
- `/reset` resets the currently active provider bucket for the active `SessionKey`.
- session identity is transport-aware: `SessionKey(transport, chat_id, topic_id)`.
- `/model` inside a topic updates only that topic session (not global config).
- task tools now support permanent single-task removal via `delete_task.py` (`/tasks/delete`).
- `create_task.py --priority interactive|background|batch` controls whether a task bypasses the per-chat concurrency cap.
- `ask_agent_async.py` supports `--reply-to AGENT` and `--silent` for automated multi-agent pipelines.
- task routing is topic-aware via `thread_id` and `DUCTOR_TOPIC_ID`.
- API auth accepts optional `channel_id` for per-channel session isolation.
- startup recovery uses `inflight_turns.json` + recovered named sessions.
- auth allowlists (`allowed_user_ids`, `allowed_group_ids`) are hot-reloadable.
- `ductor agents add` is a Telegram-focused scaffold; Matrix sub-agents are supported through `agents.json` or the bundled agent tool scripts.

Continue with `docs/system_overview.md` and `docs/architecture.md` for complete runtime detail.
