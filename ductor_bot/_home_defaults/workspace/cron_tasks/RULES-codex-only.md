# Cron Tasks

This directory contains isolated task folders used by scheduled jobs.
For cron tool commands (add/edit/remove/list), see `tools/cron_tools/CLAUDE.md`.

## ⚠️ MANDATORY WORKFLOW: Creating Cron Jobs

**CRITICAL: When creating a new cron job, you MUST ALWAYS ask the user these questions:**

1. **Which model?** (`--model <name>`)
   - Options: `gpt-5.2-codex` (recommended), `gpt-5.3-codex`, `gpt-5.1-codex-max`, `gpt-5.2`, `gpt-5.1-codex-mini`
   - Default if user doesn't specify: Use global config model

2. **Which thinking level?** (`--reasoning-effort <level>`)
   - Options: `low`, `medium` (default), `high`, `xhigh`
   - Note: `gpt-5.1-codex-mini` only supports `medium` and `high`
   - Default if user doesn't specify: `medium` (model default)

**YOU MUST present these options to the user and wait for their answers BEFORE calling cron_add.py!**

**Advanced: CLI Parameters**
If the user explicitly requests additional CLI flags, use `--cli-parameters '<json-array>'`.
DO NOT suggest this proactively - only use if the user asks for it.

**Example conversation flow:**

User: "Create a cron job to analyze data every hour"

You: "I'll create a cron job to analyze data every hour. Let me configure the execution:

1. **Model**: Which Codex model?
   - `gpt-5.2-codex` (recommended, balanced performance)
   - `gpt-5.3-codex` (latest, most capable)
   - `gpt-5.1-codex-max` (optimized for deep reasoning)
   - `gpt-5.2` (latest frontier model)
   - `gpt-5.1-codex-mini` (faster, cheaper, limited reasoning)

2. **Thinking level**: How deeply should it reason?
   - `low` (fast, surface-level)
   - `medium` (default, balanced)
   - `high` (extended thinking)
   - `xhigh` (maximum reasoning depth)

Please specify your choices, or I'll use global config defaults."

[Wait for user response, then call cron_add.py with appropriate flags]

## Important Context

Each cron run starts a fresh agent session in `cron_tasks/<task-folder>/`.
That sub-agent has no Telegram chat history and no main-session context.

## Task Folder Structure

```text
cron_tasks/<name>/
  AGENTS.md            # fixed task rules (do not edit)
  TASK_DESCRIPTION.md  # task instructions (edit this)
  <name>_MEMORY.md     # task-local memory
  scripts/             # task-specific helpers
```

## Editing Rules

- Edit behavior in `TASK_DESCRIPTION.md`.
- Keep jobs edited in place (`cron_edit.py`), do not recreate unless required.
- Do not edit task-folder `AGENTS.md` manually.
- Do not manually delete task folders; use `cron_remove.py`.

## Memory During Setup

While creating/editing cron or webhook-triggered tasks, update
`memory_system/MAINMEMORY.md` silently with user preference signals and inferred interests.

## Per-Task Execution Overrides

Each cron task can override global config settings in `cron_jobs.json`:

- `model`: Model name (optional, defaults to global config)
  - Available models:
    - `"gpt-5.2-codex"` - Frontier agentic coding model
    - `"gpt-5.3-codex"` - Latest frontier agentic coding model
    - `"gpt-5.1-codex-max"` - Codex-optimized for deep and fast reasoning
    - `"gpt-5.2"` - Latest frontier model
    - `"gpt-5.1-codex-mini"` - Cheaper, faster (limited reasoning)
- `reasoning_effort`: Thinking level (optional, defaults to `"medium"`)
  - Most models: `"low"`, `"medium"`, `"high"`, `"xhigh"`
  - `gpt-5.1-codex-mini`: `"medium"`, `"high"` only
- `cli_parameters`: List of additional CLI flags (optional, advanced users only)

**Fallback behavior:**
- If a field is `null` or missing, the global config value is used
- This allows per-task customization while maintaining global defaults
- CLI parameters are merged: global provider-specific params + task-specific params

**Example:**
```json
{
  "id": "data-analyzer",
  "schedule": "0 8 * * *",
  "task_folder": "analyzer",
  "agent_instruction": "Analyze daily data with extended thinking",
  "model": "gpt-5.2-codex",
  "reasoning_effort": "high"
}
```

**Use cases:**
- High-reasoning analysis: `"reasoning_effort": "high"`
- Fast iteration with mini: `"model": "gpt-5.1-codex-mini"`, `"reasoning_effort": "medium"`
- Advanced CLI flags: `"cli_parameters": [...]` (only if user explicitly requests)
