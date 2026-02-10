# Automation quickstart

ductor can do things without you typing. There are four background systems, and they work differently.

| System | Trigger | What runs | Where it runs |
|---|---|---|---|
| **Cron jobs** | Time (schedule) | Context-isolated subagent | Own workspace folder |
| **Webhooks** | HTTP request (event) | Main agent *or* context-isolated subagent | Active chat *or* own folder |
| **Heartbeat** | Timer (periodic) | Main agent | Active chat session |
| **Cleanup** | Daily maintenance window | Retention cleanup | `telegram_files/` + `output_to_user/` |

Cron, webhook, and heartbeat systems post results into Telegram. Cleanup runs silently.

---

## Cron jobs - do something on a schedule

Cron jobs are time-based tasks. "Every morning at 8", "every Monday at noon", "every 6 hours". You tell the agent what you want, and it sets everything up.

### How to create one

Just tell the agent in plain language:

> "Check Hacker News every morning at 8 and send me the top AI stories"

The agent will:
1. Check your timezone (asks you if it's not set yet)
2. Create the cron job entry
3. Create a task folder with a `TASK_DESCRIPTION.md`
4. Confirm the schedule

You can also be more specific:

> "Create a cron job called weather-report that runs at 7:30 every weekday and tells me the weather in Berlin"

### What happens when a cron job runs

ductor starts a fresh agent session inside the task folder. This subagent is context-isolated:

- It has no access to your main conversation
- It has no access to your main memory
- It has its own memory file that persists between runs
- It starts in its own task folder (`workspace/cron_tasks/<task_folder>`)

Filesystem hard-isolation depends on your execution mode (`docker.enabled` / provider permissions). Without sandboxing, the CLI can still access host files.

```
~/.ductor/workspace/cron_tasks/weather-report/
    CLAUDE.md                  # Agent rules (don't edit)
    AGENTS.md                  # Same rules for Codex (don't edit)
    TASK_DESCRIPTION.md        # What the subagent should do (edit this)
    weather-report_MEMORY.md   # The subagent's own memory
    scripts/                   # Helper scripts if the agent creates any
```

The subagent reads `TASK_DESCRIPTION.md`, does the work, updates its memory, and posts the result into your Telegram chat.

### Editing a cron job

To change **what** the job does: edit `TASK_DESCRIPTION.md` in the task folder, or ask the agent to do it.

To change **when** it runs: tell the agent "change the weather-report schedule to 8:00" or "disable the weather-report job".

### Advanced execution overrides (per job)

Each cron job can optionally override execution settings in `cron_jobs.json`:

```json
{
  "provider": "codex",
  "model": "gpt-5.2-codex",
  "reasoning_effort": "high",
  "cli_parameters": ["--chrome"]
}
```

- Omit a field to use the global config value.
- `reasoning_effort` is only used for Codex models that support it.
- `cli_parameters` are passed to that job's command before `--`.

### Timezones

Cron schedules run in your timezone. ductor asks for it during setup and stores it in config as `user_timezone`. If you move or want to change it, tell the agent or edit `config.json`.

Each job can also have its own timezone override if needed.

### Managing jobs

From Telegram:
- `/cron` lists all jobs with their schedule and last run status
- Tell the agent to edit, disable, or remove a job

Behind the scenes, the agent uses CLI tools in `~/.ductor/workspace/tools/cron_tools/` to manage everything. The tools handle both the JSON registry and the task folders atomically - you never need to touch files manually.

---

## Webhooks - do something when an event happens

Webhooks let external services trigger your agent. GitHub pushes, Stripe payments, monitoring alerts, email notifications - anything that can send an HTTP POST request.

### Two modes

**Wake mode** - injects a message into your active chat session, as if you typed it yourself. Your main agent handles it with full context and memory.

**Cron task mode** - runs a context-isolated subagent in a task folder, just like a cron job. The subagent has no access to your main conversation.

### How to create one

Tell the agent:

> "Create a webhook that listens for GitHub push events and tells me what changed"

The agent will:
1. Create the webhook entry with a unique ID and auth mode (`bearer` or `hmac`)
2. Pick the right mode (wake for notifications, cron_task for heavier processing)
3. Set up a prompt template
4. Tell you the endpoint URL and required auth details

### Wake mode in practice

Your webhook endpoint:
```
POST http://your-server:8742/hooks/github-push
Authorization: Bearer <your-token>
Content-Type: application/json
```

When the request arrives, ductor renders the prompt template with the payload data and injects it into your active Telegram chat. Your main agent sees it, has your full conversation history and memory, and responds.

Good for: notifications, quick questions, anything where you want the agent to have context about what you've been talking about.

### Cron task mode in practice

Same endpoint, but the webhook points to a task folder. A fresh subagent session starts, reads its `TASK_DESCRIPTION.md`, processes the payload, and posts the result to your chat.

Good for: heavy processing, data analysis, tasks that don't need conversation context.

In `cron_task` mode, a webhook can also override execution settings (`provider`, `model`, `reasoning_effort`, `cli_parameters`) exactly like cron jobs.

### Prompt templates

Templates use `{{field}}` placeholders that get filled from the JSON payload:

```
CI pipeline {{status}} on branch {{ref}}: {{commit_message}}
```

If a field is missing from the payload, it shows as `{{?field}}` instead of crashing.

### Authentication

Each webhook has its own auth. Two options:

- **Bearer token** (default) - validates hook token (or global `webhooks.token` fallback). Send it in `Authorization: Bearer <token>`
- **HMAC** - for services like GitHub that sign payloads. Supports SHA-256, SHA-1, SHA-512

### Exposing webhooks to the internet

ductor's webhook server binds to `127.0.0.1:8742` by default (localhost only). To receive webhooks from external services, you need to expose it. Options:

```bash
# Cloudflare Tunnel (free, recommended)
cloudflared tunnel --url http://localhost:8742

# Or use your reverse proxy (nginx, caddy, etc.)
```

### Managing webhooks

Tell the agent to list, edit, test, disable, or remove webhooks. The agent uses tools in `~/.ductor/workspace/tools/webhook_tools/` to manage them.

You can also test a webhook locally:

> "Test the github-push webhook with a sample payload"

---

## Heartbeat - periodic check-ins

The heartbeat system lets the agent speak up on its own during active sessions. Instead of waiting for you to ask, the agent periodically gets a prompt like "anything on your mind?" and can respond if it has something to say.

### How it works

1. Every N minutes (default: 30), ductor checks each active session
2. If you haven't been active recently (cooldown), it sends the heartbeat prompt to the agent
3. The agent can respond with a message, or stay quiet
4. If the agent responds, the message appears in your Telegram chat
5. During quiet hours, heartbeats are paused entirely

The agent runs inside your existing session with full context and memory. It's not a subagent - it's the same agent that knows your conversation.

### Enable it

Heartbeat is off by default. Tell the agent to turn it on, or edit your config:

```json
{
  "heartbeat": {
    "enabled": true,
    "interval_minutes": 30,
    "cooldown_minutes": 5,
    "quiet_start": 21,
    "quiet_end": 8,
    "prompt": "Check if there's anything worth bringing up."
  }
}
```

### What the settings mean

| Setting | What it does | Default |
|---|---|---|
| `enabled` | Master switch | `false` |
| `interval_minutes` | How often to check | `30` |
| `cooldown_minutes` | Skip if you were active this recently | `5` |
| `quiet_start` | When to stop (hour, in your timezone) | `21` (9 PM) |
| `quiet_end` | When to resume (hour, in your timezone) | `8` (8 AM) |
| `prompt` | What gets sent to the agent | See config |

### Custom heartbeat prompts

The default prompt asks the agent to check in generally. You can make it more specific:

> "Review my open GitHub issues and tell me if any need attention"

> "Check the server status dashboard and report anomalies"

> "Look at my MAINMEMORY.md and suggest something I might have forgotten"

The prompt runs inside your active session, so the agent has access to everything: your conversation history, memory, tools, and files.

---

## Cleanup - automatic file retention

ductor runs a daily cleanup pass for downloaded Telegram files and generated output files.

How it works:

1. Every hour, ductor checks local time in `user_timezone`
2. At `cleanup.check_hour` (default: `3`), it runs once per day
3. It deletes top-level files older than:
   - `cleanup.telegram_files_days` (default: `30`) in `workspace/telegram_files/`
   - `cleanup.output_to_user_days` (default: `30`) in `workspace/output_to_user/`

Cleanup is non-recursive: subdirectories are left untouched.

---

## Configuration overview

All automation settings live in `~/.ductor/config/config.json`:

```json
{
  "user_timezone": "Europe/Berlin",

  "heartbeat": {
    "enabled": false,
    "interval_minutes": 30,
    "cooldown_minutes": 5,
    "quiet_start": 21,
    "quiet_end": 8
  },

  "cleanup": {
    "enabled": true,
    "telegram_files_days": 30,
    "output_to_user_days": 30,
    "check_hour": 3
  },

  "webhooks": {
    "enabled": false,
    "host": "127.0.0.1",
    "port": 8742,
    "rate_limit_per_minute": 30
  },

  "cli_parameters": {
    "claude": [],
    "codex": ["--chrome"]
  }
}
```

Cron jobs are stored separately in `~/.ductor/cron_jobs.json` and managed through the agent's tools. Webhooks are stored in `~/.ductor/webhooks.json`.

You don't need to edit these files manually. Tell the agent what you want and it handles the rest. But if you prefer, everything is plain JSON and Markdown - you can always edit directly.
