# Ductor Home

This is the top-level `~/.ductor` directory.
The main Telegram assistant usually runs with cwd `workspace/`.

## Cold Start (No Context)

Read in this order:

1. `workspace/CLAUDE.md` (main behavior + Telegram rules)
2. `workspace/tools/CLAUDE.md` (tool routing)
3. `workspace/memory_system/MAINMEMORY.md` (long-term context)
4. `config/CLAUDE.md` (only for config changes)

## Top-Level Layout

- `workspace/` - agent working area (tools, memory, cron tasks, skills, files)
- `config/config.json` - runtime configuration
- `sessions.json` - per-chat session state
- `cron_jobs.json` - cron registry
- `webhooks.json` - webhook registry
- `logs/` - runtime logs

## Operating Rules

- Use tool scripts in `workspace/tools/` for cron/webhook lifecycle changes.
Do not manually edit `cron_jobs.json` or `webhooks.json` for normal operations.
- When config changes are requested, edit only requested keys in `config/config.json`.
Then tell the user to run `/restart`.
- Save user-facing generated files in `workspace/output_to_user/` and send with
`<file:/absolute/path/to/output_to_user/...>`.
- Update `workspace/memory_system/MAINMEMORY.md` silently when durable user facts
or preferences are learned.
