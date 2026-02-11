# Webhook Tools

Scripts for managing incoming HTTP webhook endpoints.

## ⚠️ MANDATORY: Ask Before Creating cron_task Webhooks

**When creating a webhook in `cron_task` mode, you MUST ask:**

1. **Which CLI provider?**
   - `claude` - Standard Claude models
   - `codex` - OpenAI Codex models with extended thinking

2. **Which model?**
   - **If Claude:** `haiku`, `sonnet` (recommended), `opus`
   - **If Codex:** `gpt-5.2-codex` (recommended), `gpt-5.3-codex`, `gpt-5.1-codex-max`, `gpt-5.2`, `gpt-5.1-codex-mini`

3. **If Codex: Which thinking level?**
   - `low`, `medium` (default), `high`, `xhigh`
   - Note: `gpt-5.1-codex-mini` only supports `medium` and `high`

4. **Should this webhook respect quiet hours?**
   - Ask: "Should this webhook skip execution during specific hours (e.g., at night)?"
   - If YES: Ask for start/end hours (e.g., "Don't run between 22:00-08:00")
   - Explain: "Quiet hours prevent webhooks from running during specified times (default: 21:00-08:00)"
   - Use `--quiet-start <hour>` and `--quiet-end <hour>` (0-23, supports wrap-around)

5. **Does this webhook share resources with other tasks?**
   - Ask: "Does this webhook use Chrome/browser, or compete for API rate limits/tokens?"
   - If YES: "Use a dependency name (e.g., `chrome_browser`) so tasks run one at a time"
   - Explain: "Tasks with the SAME dependency run sequentially. Different dependencies run in parallel."
   - Use `--dependency <name>` (e.g., `chrome_browser`, `api_rate_limit`, `database`)

**Present these options and wait for the user's choice!**

For `wake` mode webhooks, these parameters are not applicable (uses current main session).

Do NOT suggest `--cli-parameters` proactively. Only mention it exists if the user asks.

## Mandatory Rules

1. Use webhook tool scripts for create/list/edit/remove/test/rotate.
2. Do not manually edit `~/.ductor/webhooks.json` for normal operations.
3. Use exact hook IDs from `webhook_list.py` output.
4. Run tools with `python3`.

## Runtime Model (What Happens)

Endpoint pattern:

```text
POST /hooks/<hook-id>
```

Request validation order:

1. rate limit
2. `Content-Type: application/json`
3. JSON parse (must be object)
4. hook exists and is enabled
5. per-hook auth (`bearer` or `hmac`)
6. return `202 Accepted`, process async

The framework wraps rendered payloads in untrusted boundary markers before dispatch.

## Modes

| Mode | Behavior |
|------|----------|
| `wake` | injects prompt into the main Telegram chat flow |
| `cron_task` | runs isolated execution in `cron_tasks/<task_folder>/` |

For `cron_task`, `task_folder` is required. Missing folders are scaffolded by `webhook_add.py`.

## Auth Modes

### Bearer (default)

- Per-hook token is auto-generated on create.
- Caller sends: `Authorization: Bearer <token>`.
- Legacy hooks without per-hook token can fall back to global `webhooks.token`.

### HMAC

Requires:

- `--hmac-secret`
- `--hmac-header`

Optional tuning:

- `--hmac-algorithm` (`sha256|sha1|sha512`)
- `--hmac-encoding` (`hex|base64`)
- `--hmac-sig-prefix`
- `--hmac-sig-regex`
- `--hmac-payload-prefix-regex`

## Core Commands

### Create

```bash
# bearer mode
python3 tools/webhook_tools/webhook_add.py \
  --name "email-notify" --title "Email Notify" \
  --description "Incoming email events" \
  --mode "wake" --prompt-template "New email from {{from}}: {{subject}}"

# hmac mode (example: GitHub)
python3 tools/webhook_tools/webhook_add.py \
  --name "github-pr" --title "GitHub PR" \
  --description "PR events" --mode "wake" \
  --prompt-template "PR {{action}}: {{title}}" \
  --auth-mode "hmac" --hmac-secret "<secret>" --hmac-header "X-Hub-Signature-256"

# cron_task mode - Claude example
python3 tools/webhook_tools/webhook_add.py \
  --name "email-processor" --title "Email Processor" \
  --description "Process incoming emails" \
  --mode "cron_task" --task-folder "email-processor" \
  --prompt-template "Process email from {{from}}: {{subject}}" \
  --provider claude \
  --model sonnet

# cron_task mode - Codex example
python3 tools/webhook_tools/webhook_add.py \
  --name "github-review" --title "PR Review" \
  --description "Review incoming PR payloads" \
  --mode "cron_task" --task-folder "github-review" \
  --prompt-template "Review PR #{{number}}: {{title}}" \
  --provider codex \
  --model gpt-5.2-codex \
  --reasoning-effort high
```

**Available parameters for cron_task mode:**
- `--provider` - CLI provider: `claude` or `codex` (optional)
- `--model` - Model choice (optional)
- `--reasoning-effort` - Codex only: thinking level (optional)
- `--cli-parameters` - Advanced: JSON array (only if user explicitly requests)

### List

```bash
python3 tools/webhook_tools/webhook_list.py
```

### Edit

```bash
python3 tools/webhook_tools/webhook_edit.py "hook-id" --enable
python3 tools/webhook_tools/webhook_edit.py "hook-id" --disable
python3 tools/webhook_tools/webhook_edit.py "hook-id" --prompt-template "..."
python3 tools/webhook_tools/webhook_edit.py "hook-id" --auth-mode "hmac"
python3 tools/webhook_tools/webhook_edit.py "hook-id" --regenerate-token
python3 tools/webhook_tools/webhook_edit.py "hook-id" --provider "codex"
python3 tools/webhook_tools/webhook_edit.py "hook-id" --model "gpt-5.2-codex"
python3 tools/webhook_tools/webhook_edit.py "hook-id" --reasoning-effort "high"
python3 tools/webhook_tools/webhook_edit.py "hook-id" --cli-parameters '["--verbose"]'
```

### Remove

```bash
python3 tools/webhook_tools/webhook_list.py
python3 tools/webhook_tools/webhook_remove.py "hook-id"
```

`webhook_remove.py` deletes only the hook entry, not cron task folders.

### Rotate Tokens

```bash
python3 tools/webhook_tools/webhook_rotate_token.py
python3 tools/webhook_tools/webhook_rotate_token.py "hook-id"
```

### Test

```bash
python3 tools/webhook_tools/webhook_test.py "hook-id" --payload '{"test": true}'
```

`webhook_test.py` auto-resolves hook auth mode. It requires:

- webhooks enabled in config
- bot running

## Setup Handoff (Always Tell the User)

After creating a webhook, provide:

1. endpoint URL (`https://<public-domain>/hooks/<hook-id>`)
2. required auth setup (Bearer token or HMAC secret/header)
3. content type requirement (`application/json`)
4. test command

## Public Exposure

Webhook server binds to localhost by default (`127.0.0.1:8742`).
Expose it with a tunnel or reverse proxy, for example:

```bash
cloudflared tunnel --url http://localhost:8742
```

## Debugging Quick Map

`webhook_list.py` shows `trigger_count`, `last_triggered_at`, and `last_error`.

Common `last_error` values:

- `error:folder_missing`
- `error:no_task_folder`
- `error:cli_not_found_<provider>`
- `error:timeout`
- `error:exit_<code>`
- `error:no_response` (wake mode)

HTTP statuses:

- `202` accepted
- `400` invalid JSON/body
- `401` auth failed
- `403` hook disabled
- `404` hook not found
- `415` wrong content type
- `429` rate limited

## Memory During Webhook Setup

After creating/editing webhook automation, update `memory_system/MAINMEMORY.md`
silently with inferred user workflow preferences and interests.

## Per-Webhook Execution Overrides

Webhooks in `cron_task` mode can override global config settings in `webhooks.json`:

- `provider`: `"claude"` or `"codex"` (optional, defaults to global config)
- `model`: Model name (optional, defaults to global config)
  - Claude models: `"haiku"`, `"sonnet"`, `"opus"`
  - Codex models:
    - `"gpt-5.2-codex"` - Frontier agentic coding model
    - `"gpt-5.3-codex"` - Latest frontier agentic coding model
    - `"gpt-5.1-codex-max"` - Codex-optimized for deep and fast reasoning
    - `"gpt-5.2"` - Latest frontier model
    - `"gpt-5.1-codex-mini"` - Cheaper, faster (limited reasoning)
- `reasoning_effort`: Thinking level (Codex only, optional, defaults to `"medium"`)
  - Most models: `"low"`, `"medium"`, `"high"`, `"xhigh"`
  - `gpt-5.1-codex-mini`: `"medium"`, `"high"` only
- `cli_parameters`: List of additional CLI flags (optional, advanced users only)

**Fallback behavior:**
- If a field is `null` or missing, the global config value is used
- This allows per-webhook customization while maintaining global defaults
- CLI parameters are merged: global provider-specific params + webhook-specific params

**Examples:**

Claude webhook:
```json
{
  "id": "email-notify",
  "mode": "cron_task",
  "task_folder": "email-processor",
  "prompt_template": "Process email from {{from}}",
  "provider": "claude",
  "model": "opus"
}
```

Codex webhook:
```json
{
  "id": "data-analyzer",
  "mode": "cron_task",
  "task_folder": "data-analysis",
  "prompt_template": "Analyze data: {{summary}}",
  "provider": "codex",
  "model": "gpt-5.2-codex",
  "reasoning_effort": "high"
}
```

**Use cases:**
- High-reasoning analysis: `"reasoning_effort": "high"` (Codex only)
- Provider-specific webhook: `"provider": "codex"` while main agent uses Claude
- Webhook-specific model: Different model per webhook
- Advanced CLI flags: `"cli_parameters": [...]` (only if explicitly needed)
