<p align="center">
  <img src="https://raw.githubusercontent.com/PleasePrompto/ductor/main/ductor_bot/bot/ductor_images/welcome.png" alt="ductor" width="200" />
</p>

<h1 align="center">ductor</h1>

<p align="center">
  <strong>ductor runs Claude Code and Codex CLI as your personal assistant on Telegram.</strong><br>
  Persistent memory. Scheduled tasks. Live streaming. Docker sandboxing.<br>
  Uses only the official CLIs. Nothing spoofed, nothing proxied.
</p>

<p align="center">
  <a href="https://pypi.org/project/ductor/"><img src="https://img.shields.io/pypi/v/ductor?color=blue" alt="PyPI" /></a>
  <a href="https://pypi.org/project/ductor/"><img src="https://img.shields.io/pypi/pyversions/ductor" alt="Python" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/PleasePrompto/ductor" alt="License" /></a>
</p>

---

## What is ductor?

ductor is a personal AI agent you talk to through Telegram. It runs on your machine, uses your existing Claude or Codex subscription, and remembers who you are between conversations. You give it a personality, teach it your preferences, and control it from your pocket - wherever you are.

No databases. No cloud services. No complicated setup. One Markdown file is the agent's entire memory. One folder (`~/.ductor/`) holds everything. Pure Python, `pipx install`, done.

You can set up cron jobs that work while you sleep, webhooks that react when something happens externally, and heartbeat prompts where the agent checks in on its own without being asked. The response streams live into your Telegram chat, and your session picks up right where you left it - even after a restart.

If you want a focused, single-user AI assistant that lives on your server and fits in your pocket, this is it.

## Quick Start

```bash
pipx install ductor
ductor
```

The setup wizard walks you through the rest.

## Why ductor?

You want to talk to Claude Code or Codex from your phone, from a tablet, or while you're away from your desk. Maybe you want scheduled tasks running overnight or webhooks that wake your agent when a GitHub PR lands. And you don't want to get your account banned in the process.

Other bots have gotten users suspended because they intercept OAuth tokens and forge API requests to impersonate the official CLI. ductor doesn't do that.

- Spawns the real CLI binary as a subprocess. No token interception, no request forging
- Uses only official rule files: `CLAUDE.md` and `AGENTS.md`
- Memory is one Markdown file. No RAG, no vector stores
- One channel (Telegram), one Python package, one command

ductor makes the CLIs reachable through Telegram, gives them a memory they can actually use, and lets you automate things that would otherwise need you sitting at a terminal.

## There are other bots. Why build another one?

I tried a bunch of CLI wrappers and Telegram bots for Claude and Codex. Most were either too complex to set up, too hard to modify, or got people banned because they spoofed headers and abused APIs. I wanted something that just uses the official CLIs the way they were meant to be used.

The agents are good enough now that you can steer them through their own rule files (`CLAUDE.md`, `AGENTS.md`). I don't need a RAG system to store memories - a single Markdown file that keeps track of what I like, what I don't, and what I'm working on is plenty. The agents still work the way they're supposed to, with their own skill sets, and I can reach them from Telegram instead of a terminal.

I picked Python because it's easy to modify. The agents can write their own automations in Python, receive webhooks (new email? parse it and ping me), set up scheduled tasks - really the only limit is your own creativity. All of that, controlled from a chat app on your phone. I like small comforts like the inline buttons the agents add to their replies. The rest I do by just talking to them.

That's it. That's why ductor exists.

## Features

### Core

- Responses stream in real-time. ductor edits the Telegram message live as text comes in
- Switch between Claude Code and Codex mid-conversation with `/model`
- Sessions survive bot restarts. Pick up where you left off
- Type `@opus explain this` to temporarily switch model without changing your default
- Send images, PDFs, voice messages, or videos. ductor routes them to the right processing tool
- Agents can send `[button:Yes]` `[button:No]` inline keyboards back to you
- Persistent memory across sessions, stored in one Markdown file the agent reads and writes

### Automation

- **Cron jobs** - schedule recurring tasks with cron expressions and timezone support. Each job is its own subagent with a dedicated workspace, task description, and memory file. Results get posted back into your Telegram chat
- **Webhooks** - HTTP endpoints with Bearer or HMAC auth. Two modes: *wake* sends a prompt straight into your active chat (like you typed it yourself), *cron_task* runs an isolated task folder. Works with GitHub, Stripe, or anything that can send a POST request
- **Heartbeat** - the agent checks in periodically during active sessions. If it has an idea or a suggestion, it speaks up. Quiet hours are respected so it won't ping you at 3 AM

#### Example: a cron job

You tell the agent: "Check Hacker News every morning at 8 and send me the top AI stories."

ductor creates a task folder with everything the subagent needs:

```
~/.ductor/workspace/cron_tasks/hn-ai-digest/
    CLAUDE.md              # Agent rules (managed by ductor, don't edit)
    TASK_DESCRIPTION.md    # What the agent should do (you edit this)
    hn-ai-digest_MEMORY.md # The subagent's own memory across runs
    scripts/               # Helper scripts if needed
```

At 8:00 every morning, ductor starts a fresh agent session inside that folder. The subagent reads `TASK_DESCRIPTION.md`, does the work, writes what it learned to its own memory file, and posts the result into your Telegram chat. It runs completely isolated - no access to your main conversation or your main memory.

#### Example: a webhook wake call

Say your CI pipeline fails. You have a webhook in *wake* mode pointing at ductor. When the POST request arrives, ductor injects it as a message into your active chat - as if you typed it yourself. Your main agent sees it, has your full conversation history and memory, and responds right there in the chat.

```
POST /hooks/ci-failure -> "CI failed on branch main: test_auth.py::test_login timed out"
-> Your agent reads this, checks the code, and tells you what went wrong
```

### Infrastructure

- **Docker sandbox** using Debian Bookworm. Both CLIs have full file system access by default, so running them in a container keeps your host safe. Auto-builds, persists auth, maps your workspace
- `/upgrade` checks PyPI and updates with one click. Automatic restart after
- Supervisor with PID lock. Exit code 42 means "restart me"
- Prompt injection detection, path traversal checks, per-user allowlist

### Developer experience

- First-run wizard detects your CLIs, walks through config, seeds the workspace
- New config fields merge in automatically when you upgrade. Nothing breaks
- `/diagnose` dumps recent logs, `/status` shows session stats
- `/stop` kills whatever is running, `/new` clears the session

## Prerequisites

| Requirement | Details |
|---|---|
| **Python 3.11+** | `python3 --version` |
| **pipx** | `pip install pipx` (recommended) or use pip |
| **One CLI installed** | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or [Codex CLI](https://github.com/openai/codex) |
| **CLI authenticated** | `claude auth` or `codex auth` |
| **Telegram Bot Token** | Get one from [@BotFather](https://t.me/BotFather) |
| **Your Telegram User ID** | Get it from [@userinfobot](https://t.me/userinfobot) |
| Docker *(optional)* | Recommended for sandboxed execution |

> See [Installation guide](https://github.com/PleasePrompto/ductor/blob/main/docs/installation.md) for detailed platform guides (Linux, macOS, WSL, Windows, VPS hosting).

## How it works

```
You (Telegram)
    |
    v
ductor (aiogram)
    |
    ├── AuthMiddleware (user allowlist)
    ├── SequentialMiddleware (per-chat lock)
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

ductor spawns the CLI as a child process and parses its streaming output. The Telegram message gets edited live as text arrives. Sessions are stored as JSON. Background systems (cron, webhooks, heartbeat, update checks) run as asyncio tasks in the same process.

## Your workspace

Everything lives in `~/.ductor/`. One folder, nothing scattered.

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
            hn-ai-digest/        # Example: each job has its own workspace
        tools/
            cron_tools/          # Add, edit, remove, list cron jobs
            webhook_tools/       # Add, edit, remove, test webhooks
            telegram_tools/      # Process files, transcribe audio, read PDFs
            user_tools/          # Custom scripts the agent builds for you
        telegram_files/          # Downloaded media, organized by date
        output_to_user/          # Files the agent sends back to you
```

You can browse this folder at any time. Everything is plain text, JSON, or Markdown. No databases, no binary formats.

## Configuration

Config lives in `~/.ductor/config/config.json`. The wizard creates it on first run:

```bash
ductor  # wizard creates config interactively
```

The important ones: `telegram_token`, `allowed_user_ids`, `provider` (claude or codex), `default_model`, `docker.enabled`, `user_timezone`. Full schema in [docs/config.md](https://github.com/PleasePrompto/ductor/blob/main/docs/config.md).

## Commands

| Command | Description |
|---|---|
| `/new` | Start a fresh session |
| `/stop` | Abort running CLI processes |
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
| [Installation guide](https://github.com/PleasePrompto/ductor/blob/main/docs/installation.md) | Platform-specific setup (Linux, macOS, WSL, Windows, VPS) |
| [Automation quickstart](https://github.com/PleasePrompto/ductor/blob/main/docs/automation.md) | Cron jobs, webhooks, heartbeat - practical guide |
| [Configuration](https://github.com/PleasePrompto/ductor/blob/main/docs/config.md) | Full config schema and options |
| [Architecture](https://github.com/PleasePrompto/ductor/blob/main/docs/architecture.md) | System design and runtime flow |
| [Module reference](https://github.com/PleasePrompto/ductor/blob/main/docs/README.md) | Detailed docs for every subsystem |

## Disclaimer

ductor runs the official CLI binaries as provided by Anthropic and OpenAI. It does not modify API calls, spoof headers, forge tokens, or impersonate clients. Every request originates from the real CLI process.

That said, Terms of Service can change at any time. Automating CLI interactions may fall into a gray area depending on how the providers interpret their rules. We built ductor to follow the intended usage patterns, but we cannot guarantee it won't lead to account restrictions.

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
