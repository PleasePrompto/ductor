#!/usr/bin/env python3
"""Add a webhook: creates the JSON entry and optionally a cron_task folder.

The WebhookObserver detects the JSON change automatically.

Usage:
    python tools/webhook_tools/webhook_add.py \
        --name "email-notify" --title "Neue Emails" \
        --description "Zapier pingt bei eingehenden Emails" \
        --mode "wake" --prompt-template "Neue Email von {{from}}: {{subject}}"
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from _shared import CRON_TASKS_DIR, HOOKS_PATH, load_hooks_or_default, sanitize_name, save_hooks

_TUTORIAL = """\
WEBHOOK ADD -- Register a new webhook endpoint.

This tool registers a hook in webhooks.json. The WebhookObserver picks it up automatically.
Each hook gets its own unique Bearer token (auto-generated) or can use HMAC signature validation.

REQUIRED PARAMETERS:
  --name              Unique hook ID (lowercase, hyphens ok)
  --title             Short human-readable title
  --description       What this webhook does
  --mode              "wake" (resume main session) or "cron_task" (run task folder)
  --prompt-template   Template with {{field}} placeholders from the payload

OPTIONAL:
  --task-folder       Required for mode "cron_task". Name of cron_tasks/<folder>.
                      If folder does not exist, it will be created with scaffolding.
  --auth-mode         "bearer" (default) or "hmac". Bearer = we generate a token.
                      HMAC = external service signs payloads (GitHub, Stripe, PayPal).
  --hmac-secret       The external service's signing secret (required for --auth-mode hmac).
  --hmac-header       Header name containing the HMAC signature (required for --auth-mode hmac).
                      Examples: "X-Hub-Signature-256" (GitHub), "Stripe-Signature" (Stripe).

HMAC ADVANCED (only when --auth-mode hmac):
  --hmac-algorithm    Hash algorithm: sha256 (default), sha1, sha512.
  --hmac-encoding     Signature encoding: hex (default) or base64.
  --hmac-sig-prefix   Prefix to strip from signature header value before comparison.
                      Default: "sha256=". Set to "" for services with no prefix (Shopify).
  --hmac-sig-regex    Regex to extract signature from header (group 1). Overrides --hmac-sig-prefix.
                      Example for Stripe: "v1=([a-f0-9]+)"
  --hmac-payload-prefix-regex
                      Regex on header value; group 1 is prepended to body with "." separator
                      before HMAC computation. Used by Stripe/Slack where signed content is
                      "{timestamp}.{body}". Example for Stripe: "t=(\\d+)"

AUTH MODES:
  bearer (default):
    A random Bearer token is generated automatically. The external service must
    include it in every request: Authorization: Bearer <token>

  hmac:
    The external service signs the request body and sends the signature in a
    specific header. Fully configurable: algorithm, encoding, signature extraction,
    and payload construction.

SERVICE PRESETS:
  GitHub:
    --hmac-header "X-Hub-Signature-256"
    (defaults work: sha256, hex, "sha256=" prefix)

  Stripe:
    --hmac-header "Stripe-Signature" --hmac-sig-prefix ""
    --hmac-sig-regex "v1=([a-f0-9]+)" --hmac-payload-prefix-regex "t=(\\d+)"

  Shopify:
    --hmac-header "X-Shopify-Hmac-Sha256" --hmac-encoding "base64" --hmac-sig-prefix ""

  Twilio:
    --hmac-header "X-Twilio-Signature" --hmac-algorithm "sha1"
    --hmac-encoding "base64" --hmac-sig-prefix ""

EXAMPLES:
  # Bearer mode (default): token auto-generated
  python tools/webhook_tools/webhook_add.py \\
      --name "email-notify" --title "Neue Emails" \\
      --description "Zapier pingt bei eingehenden Emails" \\
      --mode "wake" --prompt-template "Neue Email von {{from}}: {{subject}}"

  # HMAC mode: GitHub webhook
  python tools/webhook_tools/webhook_add.py \\
      --name "github-pr" --title "GitHub PRs" \\
      --description "PR events from GitHub" \\
      --mode "wake" --prompt-template "PR {{action}}: {{pull_request}}" \\
      --auth-mode "hmac" --hmac-secret "your-github-secret" \\
      --hmac-header "X-Hub-Signature-256"

  # HMAC mode: Stripe webhook (custom sig extraction + timestamp payload)
  python tools/webhook_tools/webhook_add.py \\
      --name "stripe-payments" --title "Stripe Payments" \\
      --description "Payment events from Stripe" \\
      --mode "wake" --prompt-template "Payment {{type}}: {{data}}" \\
      --auth-mode "hmac" --hmac-secret "whsec_..." \\
      --hmac-header "Stripe-Signature" --hmac-sig-prefix "" \\
      --hmac-sig-regex "v1=([a-f0-9]+)" --hmac-payload-prefix-regex "t=(\\d+)"

  # Cron task mode: trigger isolated task
  python tools/webhook_tools/webhook_add.py \\
      --name "github-review" --title "PR Reviews" \\
      --description "Code-Review bei neuem PR" \\
      --mode "cron_task" --task-folder "github-review" \\
      --prompt-template "Review PR #{{number}}: {{title}}"
"""


def _create_task_folder(name: str, title: str, description: str) -> Path:
    """Create a cron_task workspace folder if it does not exist."""
    task_dir = CRON_TASKS_DIR / name
    if task_dir.exists():
        return task_dir

    task_dir.mkdir(parents=True, exist_ok=True)

    claude_content = f"""\
# Your Mission

You are an **automated agent** triggered by a webhook. Complete your task autonomously.

## Workflow

1. **Read** `{name}_MEMORY.md` first -- context from previous runs.
2. **Read** the incoming webhook prompt carefully.
3. Perform the task.
4. **Update** `{name}_MEMORY.md` with date/time and what you did.

## Rules

- Stay focused on this task only.
- Do not modify files outside this task folder.
- Use `.venv` for Python dependencies if needed.
"""
    (task_dir / "CLAUDE.md").write_text(claude_content, encoding="utf-8")
    (task_dir / "AGENTS.md").write_text(claude_content, encoding="utf-8")

    task_desc = f"""\
# {title}

## Goal

{description}

## Assignment

(Detailed instructions for this webhook-triggered task.)

## Output

(What should the result look like?)
"""
    (task_dir / "TASK_DESCRIPTION.md").write_text(task_desc, encoding="utf-8")
    (task_dir / f"{name}_MEMORY.md").write_text(f"# {name} Memory\n", encoding="utf-8")
    (task_dir / "scripts").mkdir(exist_ok=True)

    return task_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Add a webhook endpoint")
    parser.add_argument("--name", help="Unique hook ID")
    parser.add_argument("--title", help="Short human-readable title")
    parser.add_argument("--description", help="What this webhook does")
    parser.add_argument("--mode", choices=["wake", "cron_task"], help="wake or cron_task")
    parser.add_argument("--prompt-template", help="Template with {{field}} placeholders")
    parser.add_argument("--task-folder", help="cron_tasks/<folder> (required for cron_task mode)")
    parser.add_argument(
        "--auth-mode",
        choices=["bearer", "hmac"],
        default="bearer",
        help="Auth method: bearer (default, token auto-generated) or hmac (external signing)",
    )
    parser.add_argument("--hmac-secret", help="External service's HMAC signing secret")
    parser.add_argument(
        "--hmac-header",
        help='Header name for HMAC signature (e.g. "X-Hub-Signature-256")',
    )
    parser.add_argument(
        "--hmac-algorithm",
        choices=["sha256", "sha1", "sha512"],
        default="sha256",
        help="Hash algorithm (default: sha256)",
    )
    parser.add_argument(
        "--hmac-encoding",
        choices=["hex", "base64"],
        default="hex",
        help="Signature encoding (default: hex)",
    )
    parser.add_argument(
        "--hmac-sig-prefix",
        default="sha256=",
        help='Prefix to strip from signature header (default: "sha256=", use "" for none)',
    )
    parser.add_argument(
        "--hmac-sig-regex",
        default="",
        help="Regex to extract signature (group 1), overrides --hmac-sig-prefix",
    )
    parser.add_argument(
        "--hmac-payload-prefix-regex",
        default="",
        help='Regex on header value; group 1 prepended to body with "." before HMAC',
    )
    args = parser.parse_args()

    required = ["name", "title", "description", "mode", "prompt_template"]
    missing = [p for p in required if not getattr(args, p)]
    if missing:
        print(_TUTORIAL)
        print(f"Missing: {', '.join('--' + m.replace('_', '-') for m in missing)}")
        sys.exit(1)

    if args.mode == "cron_task" and not args.task_folder:
        print(json.dumps({"error": "--task-folder is required for mode 'cron_task'"}))
        sys.exit(1)

    if args.auth_mode == "hmac":
        if not args.hmac_secret:
            print(json.dumps({"error": "--hmac-secret is required for --auth-mode hmac"}))
            sys.exit(1)
        if not args.hmac_header:
            print(json.dumps({"error": "--hmac-header is required for --auth-mode hmac"}))
            sys.exit(1)

    name = sanitize_name(args.name)
    if not name:
        print(json.dumps({"error": "Name resolves to empty after sanitization"}))
        sys.exit(1)

    data = load_hooks_or_default(HOOKS_PATH)

    if any(h["id"] == name for h in data["hooks"]):
        print(json.dumps({"error": f"Hook '{name}' already exists"}))
        sys.exit(1)

    # Create cron_task folder if needed
    folder_created = False
    task_folder = None
    if args.mode == "cron_task":
        task_folder = sanitize_name(args.task_folder)
        folder_existed = (CRON_TASKS_DIR / task_folder).exists()
        _create_task_folder(task_folder, args.title, args.description)
        folder_created = not folder_existed

    # Generate per-hook bearer token (only for bearer mode)
    token = ""
    if args.auth_mode == "bearer":
        token = secrets.token_urlsafe(32)

    hook = {
        "id": name,
        "title": args.title,
        "description": args.description,
        "mode": args.mode,
        "prompt_template": args.prompt_template,
        "enabled": True,
        "task_folder": task_folder,
        "auth_mode": args.auth_mode,
        "token": token,
        "hmac_secret": args.hmac_secret or "",
        "hmac_header": args.hmac_header or "",
        "hmac_algorithm": args.hmac_algorithm,
        "hmac_encoding": args.hmac_encoding,
        "hmac_sig_prefix": args.hmac_sig_prefix,
        "hmac_sig_regex": args.hmac_sig_regex,
        "hmac_payload_prefix_regex": args.hmac_payload_prefix_regex,
        "created_at": datetime.now(UTC).isoformat(),
        "trigger_count": 0,
        "last_triggered_at": None,
        "last_error": None,
    }
    data["hooks"].append(hook)
    save_hooks(HOOKS_PATH, data)

    result: dict = {
        "hook_id": name,
        "mode": args.mode,
        "auth_mode": args.auth_mode,
        "endpoint": f"/hooks/{name}",
        "json_entry_created": True,
    }

    if args.mode == "cron_task":
        result["task_folder"] = f"cron_tasks/{task_folder}"
        result["folder_created"] = folder_created
        if folder_created:
            result["action_required"] = [
                f"Open cron_tasks/{task_folder}/TASK_DESCRIPTION.md and fill in the Assignment section.",
            ]

    # Setup instructions for the user (CRITICAL for non-technical users)
    if args.auth_mode == "bearer":
        result["bearer_token"] = token
        result["setup_instructions"] = {
            "step_1_check_tunnel": (
                "IMPORTANT: Is a Cloudflare Tunnel running? Without it, external services "
                "cannot reach this webhook. Start one with: "
                "cloudflared tunnel --url http://localhost:8742"
            ),
            "step_2_endpoint_url": (
                f"Configure the external service to POST to: "
                f"https://<your-tunnel-domain>/hooks/{name}"
            ),
            "step_3_auth_header": (
                "The external service MUST send this header with every request:\n"
                f"  Authorization: Bearer {token}\n"
                "Copy this token now -- it is the key to authenticate requests."
            ),
            "step_4_content_type": (
                "The external service must send JSON: Content-Type: application/json"
            ),
            "step_5_test": (
                f'Test locally: python3 tools/webhook_tools/webhook_test.py "{name}" '
                f"--payload '{{\"test\": true}}'"
            ),
        }
    elif args.auth_mode == "hmac":
        result["setup_instructions"] = {
            "step_1_check_tunnel": (
                "IMPORTANT: Is a Cloudflare Tunnel running? Without it, external services "
                "cannot reach this webhook. Start one with: "
                "cloudflared tunnel --url http://localhost:8742"
            ),
            "step_2_endpoint_url": (
                f"Configure the external service to POST to: "
                f"https://<your-tunnel-domain>/hooks/{name}"
            ),
            "step_3_hmac_signing": (
                f"The external service signs request bodies with "
                f"HMAC-{args.hmac_algorithm.upper()} ({args.hmac_encoding}). "
                f"The HMAC secret has been stored. The signature will be read from "
                f"the '{args.hmac_header}' header."
            ),
            "step_4_content_type": (
                "The external service must send JSON: Content-Type: application/json"
            ),
            "step_5_test": (
                "Use the external service's test/ping feature to send a test event. "
                f'Or: python3 tools/webhook_tools/webhook_test.py "{name}" '
                f"--payload '{{\"test\": true}}'"
            ),
        }

    if name != args.name.strip():
        result["name_sanitized"] = True
        result["original_name"] = args.name.strip()

    print(json.dumps(result))


if __name__ == "__main__":
    main()
