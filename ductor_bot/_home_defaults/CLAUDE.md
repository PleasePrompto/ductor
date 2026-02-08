# Ductor Home

This is the Ductor bot's home directory. Everything the bot needs lives here.

## First 60 Seconds (No Context)

If you wake up without context, use this read order:

1. `workspace/CLAUDE.md` -- behavior, speaking style, core rules (main agent prompt)
2. `workspace/tools/CLAUDE.md` -- tool index and routing to sub-tool docs
3. `workspace/memory_system/MAINMEMORY.md` -- long-term user context
4. `config/CLAUDE.md` -- only when config changes are requested

Note: the Telegram main agent normally runs with cwd `workspace/` (not this top-level folder).

## Layout

- `workspace/` -- Agent workspace. You work here. Tools, memory, cron tasks, output files.
- `config/config.json` -- Bot configuration (see below)
- `config/CLAUDE.md` -- How to safely read/edit config settings
- `logs/` -- Framework bot logs (rotated automatically)
- `cron_jobs.json` -- Scheduled cron job definitions
- `webhooks.json` -- Webhook endpoint definitions and stats
- `sessions.json` -- Session tracking

## Configuration (`config/config.json`)

Common keys only (short list). For current full behavior, see `config/CLAUDE.md`.

| Setting | Default | What it does |
|---------|---------|--------------|
| `provider` | `"claude"` | CLI backend: `claude` or `codex` |
| `model` | `"opus"` | Default model ID (`opus`, `sonnet`, `haiku`, or Codex model IDs) |
| `permission_mode` | `"bypassPermissions"` | CLI permission mode |
| `reasoning_effort` | `"medium"` | Codex thinking level (`low`/`medium`/`high`/`xhigh`) |
| `idle_timeout_minutes` | `60` | Auto-reset session after idle |
| `user_timezone` | `""` | IANA timezone (e.g. `"Europe/Berlin"`). Affects cron, reset, quiet hours |
| `daily_reset_hour` | `4` | Hour for daily session reset (in `user_timezone`) |
| `max_turns` | `null` | Max agent turns per request |
| `max_budget_usd` | `null` | Max cost per request |
| `cli_timeout` | `600.0` | CLI subprocess timeout (seconds) |
| `streaming.enabled` | `true` | Live-stream responses to Telegram |
| `docker.enabled` | `false` | Run CLI in Docker sandbox |

Edit `config.json` directly, then `/restart` in Telegram to reload.
You may help the user change these settings when asked.
For full config behavior and safe edit rules, read `config/CLAUDE.md`.

## Output Directory

`workspace/output_to_user/` is the designated directory for all user-requested file creation
(images, PDFs, documents, exports, etc.). Always save generated files there and send them
with `<file:/path/to/output_to_user/filename>`.

## EXTREMELY IMPORTANT: Be PROACTIVE in your suggestions — don't be CURT or DISMISSIVE.

Instead, ASK the user for information. If they're unsure what to do, offer ideas, explore the system, and ask about their interests. Store EVERYTHING in MAIN MEMORY.

WITHOUT MEMORY, YOU ARE NOTHING MORE THAN A DISPOSABLE BOT.
Memory is what makes you POWERFUL. MAINMEMORY.md is the key, asking the user is the answer!

Be helpful. Be engaging. Be yourself!