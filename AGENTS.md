# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ductor is a Telegram bot that bridges messages to Claude Code CLI or OpenAI Codex CLI for AI-powered assistance. It streams responses back to Telegram with live editing, manages persistent sessions, runs scheduled cron jobs, handles inbound webhooks, and performs periodic heartbeat checks.

**Stack:** Python 3.11+, aiogram 3.x, Pydantic 2.x, asyncio, hatchling build system.

## Development Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run
ductor                  # Start bot (auto-onboarding if unconfigured)
ductor -v               # Verbose logging

# Tests
pytest                                              # All tests
pytest tests/bot/test_app.py                        # Single file
pytest tests/bot/test_app.py::test_function_name    # Single test
pytest -k "test_pattern"                            # By pattern
pytest --cov=ductor_bot --cov-report=term-missing   # With coverage

# Quality (all must pass with zero warnings)
ruff format .
ruff check .
mypy ductor_bot
```

## Architecture

### Runtime Flow

```
Telegram Update → aiogram Router → AuthMiddleware → SequentialMiddleware (per-chat lock + queue tracking)
  → TelegramBot handler → Orchestrator → CLIService → Claude/Codex subprocess
  → Streamed response → Telegram
```

### Module Map

| Module | Purpose |
|--------|---------|
| `bot/` | Telegram frontend: aiogram handlers, streaming editors, rich sender, middleware, file browser, response formatting, forum topic support |
| `orchestrator/` | Central router: command registry, message flows, hooks, model selector, directives |
| `cli/` | CLI subprocess management: Claude/Codex providers, stream event parsing, process registry |
| `session/` | Per-chat session lifecycle, JSON persistence (`sessions.json`) |
| `cron/` | In-process scheduler: cron expression evaluation, timezone-aware execution |
| `heartbeat/` | Periodic background checks in active sessions during non-quiet hours |
| `webhook/` | HTTP ingress (`/hooks/{hook_id}`): bearer/HMAC auth, wake and cron_task modes |
| `workspace/` | Path resolution (`DuctorPaths`), home seeding from `_home_defaults/`, rule-file sync, skill directory sync |
| `security/` | Prompt injection detection, path traversal validation |
| `infra/` | PID lock, restart sentinel, version checking, auto-updater |

### Key Design Patterns

- **DuctorPaths** (`workspace/paths.py`): Frozen dataclass, single source of truth for all filesystem paths. All modules derive paths from it.
- **Zone-based workspace sync** (`workspace/init.py`): Zone 2 files (CLAUDE.md, AGENTS.md) are always overwritten on update. Zone 3 files are seeded once and user-owned thereafter. Bundled skills are symlinked from the package (not copied) so they auto-update with the installed version.
- **Stream fallback**: Streaming auto-falls-back to non-streaming on error. No data loss. The streaming loop checks `was_aborted()` on each event so `/stop` breaks out immediately (cross-platform).
- **ContextVar logging** (`log_context.py`): Async-safe log enrichment with `[op:chat_id:session_id]` prefix. Operations: `msg`, `cb`, `cron`, `hb`, `wh`.
- **Process registry** (`cli/process_registry.py`): Tracks active subprocesses per chat_id for abort/kill. On Windows, uses `taskkill /F /T` for process tree termination (cmd.exe + child node.exe).
- **Message queue tracking** (`bot/middleware.py`): Tracks pending messages per chat with `[Message in queue...]` indicators, individual cancel buttons (`mq:` callbacks), and bulk drain on `/stop`.
- **Message hooks** (`orchestrator/hooks.py`): Condition-based prompt suffixes (e.g., memory reminder every 6th message) without modifying core flow.
- **Provider abstraction** (`cli/claude_provider.py`, `cli/codex_provider.py`): Same CLIService interface for both Claude Code and Codex CLI. On Windows, prompts are passed via stdin instead of command-line arguments to avoid `.cmd` wrapper character mangling (shared helpers `_IS_WINDOWS`, `_win_stdin_pipe()`, `_win_feed_stdin()` in `base.py`).
- **Shared response formatting** (`bot/response_format.py`): `SEP`, `fmt()`, shared text constants for `/new` and `/stop`. All command responses use consistent title/separator/body layout.
- **File browser** (`bot/file_browser.py`): Interactive `~/.ductor/` navigator via inline keyboard. Callback encoding: `sf:<rel_path>` (directory nav), `sf!<rel_path>` (file request to agent). Edits messages in-place on button press.
- **Forum topic support** (`bot/topic.py`): `get_thread_id(message)` extracts `message_thread_id` when `is_topic_message` is True. All outgoing `bot.send_message/send_photo/send_document/send_chat_action` calls propagate `message_thread_id` so the bot works in Telegram groups with forum topics enabled. Sessions remain keyed by `chat_id` (no per-topic isolation). Background systems (cron, heartbeat, webhook) target private chats and pass `thread_id=None`.
- **Command ownership**: `/start`, `/help`, `/info`, `/showfiles`, `/stop`, `/restart`, `/new` are handled directly by `bot/app.py`. `/status`, `/memory`, `/model`, `/cron`, `/diagnose`, `/upgrade` route through the orchestrator command registry. `/showfiles` and other read-only commands bypass the per-chat lock via `QUICK_COMMANDS`.

### Background Systems

All run in-process as asyncio tasks, managed by the Orchestrator:
- **CronObserver**: Polls `cron_jobs.json` mtime, schedules jobs via cronsim.
- **HeartbeatObserver**: Periodic checks with cooldown and quiet-hour awareness.
- **WebhookObserver**: aiohttp server with per-hook auth and rate limiting.
- **CleanupObserver**: Daily retention cleanup for `telegram_files/` and `output_to_user/` (configurable age + check hour).
- **UpdateObserver**: PyPI version check every 60 minutes.
- **Rule sync task**: Mirrors CLAUDE.md/AGENTS.md by mtime.
- **Skill sync task**: Three-way symlink sync between ductor/Claude/Codex skill directories (30s interval). Bundled skills linked from package, external user symlinks protected. Ductor-created symlinks cleaned up on shutdown via `cleanup_ductor_links()`.

### Error Hierarchy

`DuctorError` (base) → `CLIError`, `SessionError`, `CronError`, `StreamError`, `SecurityError`, `PathValidationError`, `WebhookError` (all in `errors.py`).

### Data Persistence

All JSON files use atomic writes (temp file + rename). Located in `~/.ductor/`:
- `config/config.json` — Bot configuration (deep-merged with Pydantic defaults on startup)
- `sessions.json` — Active sessions per chat_id
- `cron_jobs.json` — Scheduled tasks
- `webhooks.json` — Webhook definitions

### Home Defaults

`ductor_bot/_home_defaults/` contains the template seeded to `~/.ductor/` at startup. Ruff and mypy skip this directory. Files here are runtime workspace content, not library code.

## Conventions

- **asyncio_mode = "auto"**: All async test functions run automatically without `@pytest.mark.asyncio`.
- **Line length 100**, double quotes, Google-style docstrings (enforced by Ruff).
- **Ruff selects ALL rules** with specific ignores — check `pyproject.toml` `[tool.ruff.lint]` before adding rule suppressions.
- **mypy strict mode** with overrides for third-party modules (aiogram, cronsim, yaml, questionary) and `_home_defaults`.
- **Config merge**: User config is deep-merged with Pydantic defaults so new fields are added transparently on update.
- **Supervisor restart**: Exit code 42 signals the supervisor (`run.py`) to restart immediately.
