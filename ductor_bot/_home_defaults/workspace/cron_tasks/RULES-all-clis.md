# Cron Tasks

This directory contains isolated task folders used by scheduled jobs.
For cron tool commands (add/edit/remove/list), see `tools/cron_tools/CLAUDE.md`.

## MANDATORY WORKFLOW: Creating Cron Jobs

**CRITICAL: When creating a new cron job, you MUST ALWAYS ask the user these questions:**

1. **Which CLI provider?** (`--provider claude`, `--provider codex`, or `--provider gemini`)
   - Default if user doesn't specify: Use global config provider

2. **Which model?** (`--model <name>`)
   - Claude models: `haiku`, `sonnet`, `opus`
   - Codex models: `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.2`
   - Gemini models: `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-3-pro-preview`, `gemini-3-flash-preview`, `gemini-3.1-pro-preview`
   - Default if user doesn't specify: Use global config model

3. **If Codex provider: Which thinking level?** (`--reasoning-effort <level>`)
   - Options: `low`, `medium`, `high`, `xhigh`
   - Default if user doesn't specify: `medium` (model default)

**YOU MUST present these options to the user and wait for their answers BEFORE calling cron_add.py!**

**Advanced: CLI Parameters**
If the user explicitly requests additional CLI flags, use `--cli-parameters '<json-array>'`.
DO NOT suggest this proactively - only use if the user asks for it.

**Example conversation flow:**

User: "Create a cron job to check weather every 3 minutes"

You: "I'll create a cron job to check weather every 3 minutes. Let me configure the execution:

1. **Provider**: Which CLI should execute this task?
   - `claude` (standard Claude models)
   - `codex` (OpenAI Codex models with extended thinking)
   - `gemini` (Google Gemini models)

2. **Model**: Which model?
   - If Claude: `haiku` (fast), `sonnet` (balanced), `opus` (most capable)
   - If Codex: `gpt-5.4` (recommended), `gpt-5.4-mini`, `gpt-5.3-codex`, etc.
   - If Gemini: `gemini-2.5-pro` (recommended), `gemini-2.5-flash`, `gemini-2.5-flash-lite`, etc.

3. **Thinking level** (Codex only): How deeply should it reason?
   - `low`, `medium` (default), `high`, `xhigh`

Please specify your choices, or I'll use global config defaults."

[Wait for user response, then call cron_add.py with appropriate flags]

## Important Context

Each cron run starts a fresh agent session in `cron_tasks/<task-folder>/`.
That sub-agent has no Telegram chat history and no main-session context.

## Task Folder Structure

```text
cron_tasks/<name>/
  CLAUDE.md            # fixed task rules (do not edit)
  AGENTS.md            # mirror of CLAUDE.md (do not edit)
  GEMINI.md            # mirror of CLAUDE.md (do not edit)
  TASK_DESCRIPTION.md  # task instructions (edit this)
  <name>_MEMORY.md     # task-local memory
  scripts/             # task-specific helpers
```

## Editing Rules

- Edit behavior in `TASK_DESCRIPTION.md`.
- Keep jobs edited in place (`cron_edit.py`), do not recreate unless required.
- Do not edit task-folder `CLAUDE.md`, `AGENTS.md`, or `GEMINI.md` manually.
- Do not manually delete task folders; use `cron_remove.py`.

## Memory During Setup

While creating/editing cron or webhook-triggered tasks, update
`memory_system/MAINMEMORY.md` silently with user preference signals and inferred interests.

## Per-Task Execution Overrides

Each cron task can override global config settings in `cron_jobs.json`:

- `provider`: `"claude"`, `"codex"`, or `"gemini"` (optional, defaults to global config)
- `model`: Model name (optional, defaults to global config)
  - Claude models: `"haiku"`, `"sonnet"`, `"opus"`
  - Codex models:
    - `"gpt-5.4"` - Latest frontier agentic coding model
    - `"gpt-5.4-mini"` - Smaller, faster frontier coding model
    - `"gpt-5.3-codex"` - Frontier Codex-optimized coding model
    - `"gpt-5.2"` - Optimized for long-running agent work
  - Gemini models:
    - `"gemini-2.5-pro"` - Balanced, most capable
    - `"gemini-2.5-flash"` - Fast and cost-effective
    - `"gemini-2.5-flash-lite"` - Cheapest, fastest
    - `"gemini-3-pro-preview"` - Next-gen preview
    - `"gemini-3-flash-preview"` - Next-gen fast preview
    - `"gemini-3.1-pro-preview"` - Latest preview
- `reasoning_effort`: Thinking level (Codex only, optional, defaults to `"medium"`)
  - Typical values: `"low"`, `"medium"`, `"high"`, `"xhigh"`
- `cli_parameters`: List of additional CLI flags (optional, advanced users only)

**Fallback behavior:**
- If a field is `null` or missing, the global config value is used
- This allows per-task customization while maintaining global defaults
- CLI parameters are merged: global provider-specific params + task-specific params

**Examples:**

Claude task:
```json
{
  "id": "daily-summary",
  "schedule": "0 8 * * *",
  "task_folder": "summary",
  "agent_instruction": "Summarize daily reports",
  "provider": "claude",
  "model": "opus"
}
```

Codex task:
```json
{
  "id": "data-analyzer",
  "schedule": "0 8 * * *",
  "task_folder": "analyzer",
  "agent_instruction": "Analyze daily data with extended thinking",
  "provider": "codex",
  "model": "gpt-5.4",
  "reasoning_effort": "high"
}
```

Gemini task:
```json
{
  "id": "report-generator",
  "schedule": "0 8 * * *",
  "task_folder": "reports",
  "agent_instruction": "Generate daily report",
  "provider": "gemini",
  "model": "gemini-2.5-pro"
}
```

**Use cases:**
- High-reasoning analysis (Codex only): `"reasoning_effort": "high"`
- Provider-specific task: `"provider": "gemini"` while main agent uses Claude
- Task-specific model: Different model per cron job
- Advanced CLI flags: `"cli_parameters": [...]` (only if user explicitly requests)
