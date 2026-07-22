# Agent Tools

Tools for inter-agent communication and shared knowledge.
Agent creation and removal are only available on the main agent.

## How users interact with sub-agents

Each sub-agent is a **separate bot** (Telegram or Matrix) with its own chat.
The user has two options:

1. **Direct chat** — The user opens the sub-agent's bot and chats directly.
   This is the primary way to use a sub-agent.
2. **Delegation** — The user asks the main agent to send a task via agent tools.
   The response comes back to the main agent's chat, NOT the sub-agent's chat.

**After creating a sub-agent, tell the user to open its chat to talk to it
directly.** Do NOT show `python3 tools/...` commands to the user — those are
internal tools for agent-to-agent communication only.

## Available Tools (internal, not user-facing)

| Tool | Purpose | Availability |
|------|---------|-------------|
| `ask_agent.py` | Ask a sub-agent a question (sync, blocks until response) | All agents |
| `ask_agent_async.py` | Give a sub-agent a task (async, response comes back to YOU) | All agents |
| `edit_shared_knowledge.py` | View or edit SHAREDMEMORY.md (synced to all agents) | All agents |
| `create_agent.py` | Create a new sub-agent (writes to `agents.json`, auto-detected) | Main only |
| `remove_agent.py` | Remove a sub-agent from the registry | Main only |
| `list_agents.py` | List all sub-agents and their configuration | Main only |

## How agent-to-agent communication works

**The response ALWAYS comes back to the calling agent.** There is no way
to make a sub-agent reply directly in its own chat via these tools.

    You (main) → send task to sub-agent → sub-agent processes → response returns to YOU

**Never tell the user** that a sub-agent will "answer in its own chat" or
"respond directly to the user" — that is not how these tools work.

## Creating Sub-Agents

When creating a sub-agent:

1. Choose a descriptive lowercase name (no spaces, e.g. `finanzius`, `researcher`)
2. Choose the transport: **Telegram** or **Matrix**
3. Use **specific model names**, not provider names:
   - Claude: `opus`, `opus[1m]`, `sonnet`, `sonnet[1m]`, `haiku`, `fable`
   - Codex: `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark` (check `config/codex_models.json`)
   - Gemini: `gemini-2.5-pro`, `gemini-2.5-flash` (check `config/gemini_models.json`)
   - Antigravity: prefer `antigravity-default`; cached display names may pass
     through, but `agy` model selection is not reliable
4. Provider is `claude`, `openai`/`codex`, `gemini`, or `antigravity`
5. The workspace is created automatically under `agents/<name>/`
6. The sub-agent starts automatically within seconds (FileWatcher)

**IMPORTANT:** Never use `codex`, `gemini`, or `antigravity` as model names — those are providers.
The `--model` must be a specific model ID from the lists above.

### Telegram Agent

Requires a bot token from @BotFather and Telegram user IDs (integers).

```bash
python3 tools/agent_tools/create_agent.py \
  --name "agent-name" \
  --token "BOT_TOKEN" \
  --users "USER_ID1,USER_ID2" \
  --provider claude \
  --model opus \
  --description "Short agent description for the join notification"
```

### Matrix Agent

Requires a Matrix account (user ID, homeserver URL). Password is optional
at creation time — it can be added to `agents.json` later before starting.

```bash
python3 tools/agent_tools/create_agent.py \
  --name "matrix-agent" \
  --transport matrix \
  --homeserver "https://matrix.example.com" \
  --user-id "@bot:matrix.example.com" \
  --allowed-users "@alice:example.com,@bob:example.com" \
  --provider claude \
  --model opus \
  --description "Short agent description"
```

**Matrix-specific flags:**

| Flag | Required | Description |
|------|----------|-------------|
| `--transport matrix` | Yes* | Selects Matrix transport (*auto-detected if `--homeserver` present) |
| `--homeserver` | Yes | Homeserver URL (must be HTTPS) |
| `--user-id` | Yes | Bot's Matrix user ID (`@localpart:domain`) |
| `--password` | No | Account password for initial login (token saved after first login) |
| `--allowed-users` | No | Comma-separated Matrix user IDs to allow (empty = all users) |
| `--allowed-rooms` | No | Comma-separated room IDs (`!id:srv`) or aliases (`#name:srv`). Empty = all rooms |

**Note:** `--token`/`--users` (Telegram) and `--homeserver`/`--user-id`/`--password`
(Matrix) are mutually exclusive. The script rejects conflicting flags.

**Credentials flow:** The bot needs either `password` or `access_token` in its
config to log in. On first login with a password, the bot saves an `access_token`
to `matrix_store/credentials.json`. After that, the password is no longer needed.

### Join Notification (`--description`)

**Always provide `--description`** when creating a sub-agent. This text is
saved as `workspace/JOIN_NOTIFICATION.md` and **automatically sent + pinned**
when the user first opens the bot chat or the bot joins a room.

Write a compact message (max ~500 chars) that tells the user:
1. What this agent does (one sentence)
2. Key commands: `/new` (fresh context), `/stop` (cancel), `/model` (switch), `/help`
3. How to start

Example:
```
--description "I'm your code review assistant. Send me code or a repo link and I'll review it.

**Commands**
- /new — Fresh conversation
- /stop — Cancel running task
- /model — Switch AI model
- /help — All commands

Send any message to begin."
```

To update an existing agent's notification, edit
`~/.ductor/agents/<name>/workspace/JOIN_NOTIFICATION.md` directly.

## Matrix Bot Account Setup

A Matrix agent needs a dedicated Matrix account. There are several ways
to create one, depending on the homeserver setup.

### Option 1: Public or Managed Homeserver

Register a new account through the homeserver's web interface or via
Element/another Matrix client. Use a descriptive username like
`ductor-bot` or `my-assistant`.

- Go to the homeserver's registration page or use Element → "Create Account"
- Choose a username and password
- Note down the full user ID: `@username:homeserver.domain`
- Note down the homeserver URL: `https://homeserver.domain`

### Option 2: Self-Hosted Synapse

Use the built-in registration tool or the Admin API.

**CLI registration:**
```bash
register_new_matrix_user -u botname -p password \
  -c /path/to/homeserver.yaml http://localhost:8008
```

### After Account Creation

1. You have: **homeserver URL**, **user ID** (`@bot:server`), **password**
2. Create the agent with `create_agent.py --transport matrix ...`
3. On first start, the bot logs in with the password and saves an access token
4. The password is not used again after the initial login

## Inter-Agent Communication

Each agent has its own memory, workspace, and session — they are independent.
Two modes are available:

### Synchronous (blocking)

Use `ask_agent.py` for quick lookups or simple questions. Your CLI turn
blocks until the sub-agent responds. The response is returned to you
directly as tool output.

```bash
python3 tools/agent_tools/ask_agent.py "agent-name" "Quick question"
```

### Asynchronous (long-running tasks)

Use `ask_agent_async.py` for tasks that take longer. Returns immediately
with a task_id. The sub-agent's response is delivered back to **your**
chat (the calling agent's chat) when ready.

```bash
python3 tools/agent_tools/ask_agent_async.py "agent-name" "Complex request that takes time"
```

Use async for code generation, analysis, research, or anything that may
take more than a few seconds. Use sync for quick lookups.

### Starting a fresh session (`--new`)

By default, follow-up messages to the same agent resume the existing
inter-agent session (context is preserved). To start a completely new
task with no prior context, use the `--new` flag:

```bash
python3 tools/agent_tools/ask_agent_async.py --new "agent-name" "Brand new task"
python3 tools/agent_tools/ask_agent.py --new "agent-name" "Brand new question"
```

### Named Sessions for inter-agent work

With source chat/topic context, each inter-agent conversation creates a
**Named Session** on the recipient agent with a sender/chat/topic-scoped name
(for example, `ia.main.t12345.xab12cd34`). Follow-up messages from the same
chat/topic reuse that session. Calls without source context use the legacy
`ia-{sender}` name.

These sessions:
- Persist across messages and survive bot restarts
- Run in the background, independent of the recipient's direct chat
- Are visible in the recipient's `/sessions` list

Async responses report the exact session name. To continue it directly in the
recipient's chat, use the reported name exactly: `@<session-name> <message>`.
Synchronous responses do not include the session name; use async or find it in
the recipient's `/sessions` list. Do not guess session names.

## Shared Knowledge

`SHAREDMEMORY.md` contains knowledge shared across all agents. Changes are
automatically synced into every agent's MAINMEMORY.md via the supervisor.

```bash
# View current shared knowledge
python3 tools/agent_tools/edit_shared_knowledge.py --show

# Append a fact
python3 tools/agent_tools/edit_shared_knowledge.py --append "New shared fact"

# Replace entire content
python3 tools/agent_tools/edit_shared_knowledge.py --set "Full new content"
```

When you learn something that is relevant to ALL agents (server facts, user
preferences, infrastructure changes), update shared knowledge instead of
only your own MAINMEMORY.md.

## Removing Sub-Agents

Removing a sub-agent stops its bot but **preserves its workspace**.
The workspace can be reused if the agent is re-created with the same name.

```bash
python3 tools/agent_tools/remove_agent.py "agent-name"
```
