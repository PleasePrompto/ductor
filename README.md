<p align="center">
  <img src="https://raw.githubusercontent.com/PleasePrompto/ductor/main/ductor_bot/bot/ductor_images/logo_text.png" alt="ductor" width="100%" />
</p>

<p align="center">
  <strong>Claude Code and Codex CLI as your personal Telegram assistant.</strong><br>
  Persistent memory. Scheduled tasks. Live streaming. Docker sandboxing.<br>
  Uses only the official CLIs. Nothing spoofed, nothing proxied.
</p>

<p align="center">
  <a href="https://pypi.org/project/ductor/"><img src="https://img.shields.io/pypi/v/ductor?color=blue" alt="PyPI" /></a>
  <a href="https://pypi.org/project/ductor/"><img src="https://img.shields.io/pypi/pyversions/ductor?v=1" alt="Python" /></a>
  <a href="https://github.com/PleasePrompto/ductor/blob/main/LICENSE"><img src="https://img.shields.io/github/license/PleasePrompto/ductor" alt="License" /></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> &middot;
  <a href="#why-ductor">Why ductor?</a> &middot;
  <a href="#features">Features</a> &middot;
  <a href="#prerequisites">Prerequisites</a> &middot;
  <a href="#how-it-works">How it works</a> &middot;
  <a href="#commands">Commands</a> &middot;
  <a href="https://github.com/PleasePrompto/ductor/tree/main/docs">Docs</a> &middot;
  <a href="#contributing">Contributing</a>
</p>

---

ductor runs on your machine, uses your existing Claude or Codex subscription, and remembers who you are between conversations. One Markdown file is the agent's memory. One folder (`~/.ductor/`) holds everything. `pipx install ductor`, done.

You can schedule cron jobs, set up webhooks, and let the agent check in on its own with heartbeat prompts. Responses stream live into Telegram. Sessions survive restarts.

<p align="center">
  <img src="https://raw.githubusercontent.com/PleasePrompto/ductor/main/docs/images/ductor-start.jpeg" alt="ductor /start screen" width="49%" />
  <img src="https://raw.githubusercontent.com/PleasePrompto/ductor/main/docs/images/ductor-quick-actions.jpeg" alt="ductor quick action buttons" width="49%" />
</p>
<p align="center">
  <sub>Left: <code>/start</code> onboarding screen &mdash; Right: Quick action buttons generated dynamically by the agent</sub>
</p>

## Quick start

```bash
pipx install ductor
ductor
```

The setup wizard walks you through the rest.

## Why ductor?

I tried a bunch of CLI wrappers and Telegram bots for Claude and Codex. Most were either too complex to set up, too hard to modify, or got people banned because they spoofed headers and forged API requests to impersonate the official CLI.

ductor doesn't do that.

- Spawns the real CLI binary as a subprocess. No token interception, no request forging
- Uses only official rule files: `CLAUDE.md` and `AGENTS.md`
- Memory is one Markdown file. No RAG, no vector stores
- One channel (Telegram), one Python package, one command

The agents are good enough now that you can steer them through their own rule files. I don't need a RAG system to store memories -a single Markdown file that tracks what I like, what I don't, and what I'm working on is plenty. I can reach them from Telegram instead of a terminal.

I picked Python because it's easy to modify. The agents can write their own automations, receive webhooks (new email? parse it and ping me), set up scheduled tasks. All controlled from your phone.

## Features

### Core

- Responses stream in real-time -ductor edits the Telegram message live as text arrives
- Switch between Claude Code and Codex mid-conversation with `/model`
- Sessions survive bot restarts
- `@opus explain this` temporarily switches model without changing your default
- Send images, PDFs, voice messages, or videos -ductor routes them to the right tool
- Agents can send `[button:Yes]` `[button:No]` inline keyboards back to you
- Persistent memory across sessions, stored in one Markdown file

### Automation

- **Cron jobs** -recurring tasks with cron expressions and timezone support. Each job runs as its own subagent with a dedicated workspace and memory file
- **Webhooks** -HTTP endpoints with Bearer or HMAC auth. Two modes: *wake* injects a prompt into your active chat, *cron_task* runs an isolated task. Works with GitHub, Stripe, or anything that sends POST
- **Heartbeat** -the agent checks in periodically during active sessions. Quiet hours respected

#### Example: a cron job

Tell the agent: "Check Hacker News every morning at 8 and send me the top AI stories."

ductor creates a task folder with everything the subagent needs:

```
~/.ductor/workspace/cron_tasks/hn-ai-digest/
    CLAUDE.md              # Agent rules (managed by ductor)
    TASK_DESCRIPTION.md    # What the agent should do
    hn-ai-digest_MEMORY.md # The subagent's own memory across runs
    scripts/               # Helper scripts if needed
```

At 8:00 every morning, ductor starts a fresh session in that folder. The subagent reads the task, does the work, writes what it learned to memory, and posts the result to your chat. Fully isolated from your main conversation.

#### Example: a webhook wake call

Your CI fails. A webhook in *wake* mode injects the payload into your active chat. Your agent sees it with full history and memory and responds.

```
POST /hooks/ci-failure -> "CI failed on branch main: test_auth.py::test_login timed out"
-> Agent reads this, checks the code, tells you what went wrong
```

### Infrastructure

- `ductor service install` -systemd on Linux, launchd on macOS, Task Scheduler for WSL. Starts on boot, restarts on crash
- Docker sandbox (Debian Bookworm) -both CLIs have full filesystem access by default, so a container keeps your host safe
- `/upgrade` checks PyPI and updates in one step. Automatic restart
- Supervisor with PID lock. Exit code 42 triggers restart
- Prompt injection detection, path traversal checks, per-user allowlist

### Developer experience

- First-run wizard detects your CLIs, walks through config, seeds the workspace
- New config fields merge automatically on upgrade
- `/diagnose` dumps recent logs, `/status` shows session stats
- `/stop` terminates the agent and discards all queued messages, `/new` clears the session
- Messages sent while the agent is working show `[Message in queue...]` with a cancel button

## Prerequisites

| Requirement | Details |
|---|---|
| Python 3.11+ | `python3 --version` |
| pipx | `pip install pipx` (recommended) or pip |
| One CLI installed | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or [Codex CLI](https://github.com/openai/codex) |
| CLI authenticated | `claude auth` or `codex auth` |
| Telegram Bot Token | From [@BotFather](https://t.me/BotFather) |
| Your Telegram User ID | From [@userinfobot](https://t.me/userinfobot) |
| Docker *(optional)* | Recommended for sandboxed execution |

> Detailed platform guides: [Installation (Linux, macOS, WSL, Windows, VPS)](https://github.com/PleasePrompto/ductor/blob/main/docs/installation.md)

## How it works

```
You (Telegram)
    |
    v
ductor (aiogram)
    |
    ├── AuthMiddleware (user allowlist)
    ├── SequentialMiddleware (per-chat lock + queue tracking)
    |
    v
Orchestrator
    |
    ├── Command Router (/new, /model, /stop, ...)
    ├── Message Flow -> CLIService -> claude / codex subprocess
    ├── CronObserver -> Scheduled task execution
    ├── HeartbeatObserver -> Periodic background checks
    ├── WebhookObserver -> HTTP endpoint server
    └── UpdateObserver -> PyPI version check
    |
    v
Streamed response -> Live-edited Telegram message
```

ductor spawns the CLI as a child process and parses its streaming output. The Telegram message gets edited live as text arrives. Sessions are stored as JSON. Background systems run as asyncio tasks in the same process.

## Workspace

Everything lives in `~/.ductor/`.

```
~/.ductor/
    config/config.json           # Bot config (token, user IDs, model, Docker, timezone)
    sessions.json                # Active sessions per chat
    cron_jobs.json               # Scheduled task definitions
    webhooks.json                # Webhook endpoint definitions
    CLAUDE.md                    # Agent rules (auto-synced)
    AGENTS.md                    # Same rules for Codex (auto-synced)
    logs/agent.log               # Rotating log file
    workspace/
        memory_system/
            MAINMEMORY.md        # The agent's long-term memory about you
        cron_tasks/              # One subfolder per scheduled job
        tools/
            cron_tools/          # Add, edit, remove, list cron jobs
            webhook_tools/       # Add, edit, remove, test webhooks
            telegram_tools/      # Process files, transcribe audio, read PDFs
            user_tools/          # Custom scripts the agent builds for you
        telegram_files/          # Downloaded media, organized by date
        output_to_user/          # Files the agent sends back to you
```

Plain text, JSON, and Markdown. No databases, no binary formats.

## Configuration

Config lives in `~/.ductor/config/config.json`. The wizard creates it on first run:

```bash
ductor  # wizard creates config interactively
```

Key fields: `telegram_token`, `allowed_user_ids`, `provider` (claude or codex), `default_model`, `docker.enabled`, `user_timezone`. Full schema in [docs/config.md](https://github.com/PleasePrompto/ductor/blob/main/docs/config.md).

## Commands

| Command | Description |
|---|---|
| `/new` | Start a fresh session |
| `/stop` | Terminate agent and discard queued messages |
| `/model` | Switch AI model (interactive keyboard) |
| `/model opus` | Switch directly to a specific model |
| `/status` | Session info, tokens, cost, auth status |
| `/memory` | View persistent memory |
| `/cron` | List scheduled tasks |
| `/upgrade` | Check for updates and upgrade |
| `/restart` | Restart the bot |
| `/diagnose` | Show recent log output |
| `/help` | Command reference |

## Documentation

| Document | Description |
|---|---|
| [Installation](https://github.com/PleasePrompto/ductor/blob/main/docs/installation.md) | Platform-specific setup (Linux, macOS, WSL, Windows, VPS) |
| [Automation](https://github.com/PleasePrompto/ductor/blob/main/docs/automation.md) | Cron jobs, webhooks, heartbeat |
| [Configuration](https://github.com/PleasePrompto/ductor/blob/main/docs/config.md) | Full config schema and options |
| [Architecture](https://github.com/PleasePrompto/ductor/blob/main/docs/architecture.md) | System design and runtime flow |
| [Module reference](https://github.com/PleasePrompto/ductor/blob/main/docs/README.md) | Per-subsystem documentation |

## Disclaimer

ductor runs the official CLI binaries from Anthropic and OpenAI. It does not modify API calls, spoof headers, forge tokens, or impersonate clients. Every request comes from the real CLI process.

Terms of Service can change. Automating CLI interactions may be a gray area depending on how providers interpret their rules. We built ductor to follow intended usage patterns, but can't guarantee it won't lead to account restrictions.

Use at your own risk. Check the current ToS before deploying:
- [Anthropic Terms of Service](https://www.anthropic.com/policies/terms)
- [OpenAI Terms of Use](https://openai.com/policies/terms-of-use)

Not affiliated with Anthropic or OpenAI.

## Contributing

```bash
git clone https://github.com/PleasePrompto/ductor.git
cd ductor
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
mypy ductor_bot
```

Zero warnings, zero errors. See [CLAUDE.md](https://github.com/PleasePrompto/ductor/blob/main/CLAUDE.md) for conventions.

## License

[MIT](https://github.com/PleasePrompto/ductor/blob/main/LICENSE)
