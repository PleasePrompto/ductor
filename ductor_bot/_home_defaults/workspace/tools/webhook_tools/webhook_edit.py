#!/usr/bin/env python3
"""Edit a webhook: modify properties in place.

Usage:
    python tools/webhook_tools/webhook_edit.py "email-notify" --disable
    python tools/webhook_tools/webhook_edit.py "email-notify" --prompt-template "..."
    python tools/webhook_tools/webhook_edit.py "email-notify" --regenerate-token
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys

from _shared import HOOKS_PATH, available_hook_ids, load_hooks_strict, save_hooks

_TUTORIAL = """\
WEBHOOK EDIT -- Modify an existing webhook in place.

USAGE:
  python tools/webhook_tools/webhook_edit.py "<hook-id>" [options]

OPTIONS:
  --enable              Enable the hook
  --disable             Disable the hook
  --title "..."         Change the title
  --description "..."   Change the description
  --prompt-template "..." Change the prompt template
  --task-folder "..."   Change the cron_task folder (cron_task mode only)
  --auth-mode "..."     Change auth mode ("bearer" or "hmac")
  --hmac-secret "..."   Set/change HMAC signing secret
  --hmac-header "..."   Set/change HMAC signature header name
  --hmac-algorithm      Hash algorithm: sha256, sha1, sha512
  --hmac-encoding       Signature encoding: hex or base64
  --hmac-sig-prefix     Prefix to strip from signature header (use "" for none)
  --hmac-sig-regex      Regex to extract signature (group 1)
  --hmac-payload-prefix-regex  Regex on header; group 1 prepended to body with "."
  --regenerate-token    Generate a new random Bearer token (bearer mode only)
  --provider "..."      Change CLI provider (claude or codex)
  --model "..."         Change model name
  --reasoning-effort    Change thinking level for Codex (low, medium, high, xhigh)
  --cli-parameters      Change CLI flags as JSON array
  --quiet-start <hour>  Start of quiet hours (0-23, webhook won't run during this time)
  --quiet-end <hour>    End of quiet hours (0-23, exclusive)
  --dependency "..."    Resource dependency (e.g. 'chrome_browser', 'api_rate_limit')
  --clear-quiet-hours   Remove quiet hour settings (use global config)
  --clear-dependency    Remove dependency (allow parallel execution)

EXAMPLES:
  python tools/webhook_tools/webhook_edit.py "email-notify" --disable
  python tools/webhook_tools/webhook_edit.py "email-notify" --prompt-template "New: {{subject}}"
  python tools/webhook_tools/webhook_edit.py "email-notify" --regenerate-token
  python tools/webhook_tools/webhook_edit.py "github-pr" --hmac-secret "new-secret-from-github"
  python tools/webhook_tools/webhook_edit.py "stripe-hook" --hmac-sig-regex "v1=([a-f0-9]+)"
  python tools/webhook_tools/webhook_edit.py "email-notify" --quiet-start 22 --quiet-end 7
  python tools/webhook_tools/webhook_edit.py "web-scraper" --dependency chrome_browser
  python tools/webhook_tools/webhook_edit.py "web-scraper" --clear-quiet-hours
  python tools/webhook_tools/webhook_edit.py "web-scraper" --clear-dependency
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Edit a webhook in place")
    parser.add_argument("hook_id", nargs="?", help="Exact hook ID")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--title")
    parser.add_argument("--description")
    parser.add_argument("--prompt-template")
    parser.add_argument("--task-folder")
    parser.add_argument("--auth-mode", choices=["bearer", "hmac"])
    parser.add_argument("--hmac-secret")
    parser.add_argument("--hmac-header")
    parser.add_argument("--hmac-algorithm", choices=["sha256", "sha1", "sha512"])
    parser.add_argument("--hmac-encoding", choices=["hex", "base64"])
    parser.add_argument("--hmac-sig-prefix")
    parser.add_argument("--hmac-sig-regex")
    parser.add_argument("--hmac-payload-prefix-regex")
    parser.add_argument(
        "--regenerate-token",
        action="store_true",
        help="Generate a new random Bearer token",
    )
    parser.add_argument(
        "--provider",
        choices=["claude", "codex"],
        help="Change CLI provider (claude or codex)",
    )
    parser.add_argument(
        "--model",
        help="Change model name",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh"],
        help="Change thinking level (Codex only)",
    )
    parser.add_argument(
        "--cli-parameters",
        help="Change CLI flags as JSON array (e.g. '[\"--verbose\"]')",
    )
    parser.add_argument(
        "--quiet-start",
        type=int,
        choices=range(24),
        metavar="HOUR",
        help="Start of quiet hours (0-23). Webhook won't run during quiet hours.",
    )
    parser.add_argument(
        "--quiet-end",
        type=int,
        choices=range(24),
        metavar="HOUR",
        help="End of quiet hours (0-23, exclusive).",
    )
    parser.add_argument(
        "--dependency",
        help="Resource dependency (e.g. 'chrome_browser'). Tasks with same dependency run sequentially.",
    )
    parser.add_argument(
        "--clear-quiet-hours",
        action="store_true",
        help="Remove quiet hour settings (use global config).",
    )
    parser.add_argument(
        "--clear-dependency",
        action="store_true",
        help="Remove dependency (allow parallel execution).",
    )
    args = parser.parse_args()

    if not args.hook_id:
        print(_TUTORIAL)
        sys.exit(1)

    if not HOOKS_PATH.exists():
        print(json.dumps({"error": "No webhooks.json file found"}))
        sys.exit(1)

    try:
        data = load_hooks_strict(HOOKS_PATH)
    except (json.JSONDecodeError, TypeError):
        print(json.dumps({"error": "Corrupt webhooks.json -- cannot parse"}))
        sys.exit(1)

    hooks = data.get("hooks", [])
    hook = next((h for h in hooks if h.get("id") == args.hook_id), None)

    if hook is None:
        available = available_hook_ids(hooks)
        print(
            json.dumps(
                {
                    "error": f"Hook '{args.hook_id}' not found",
                    "available_hooks": available,
                }
            )
        )
        sys.exit(1)

    changes: dict[str, object] = {}

    if args.enable:
        hook["enabled"] = True
        changes["enabled"] = True
    if args.disable:
        hook["enabled"] = False
        changes["enabled"] = False
    if args.title:
        hook["title"] = args.title
        changes["title"] = args.title
    if args.description:
        hook["description"] = args.description
        changes["description"] = args.description
    if args.prompt_template:
        hook["prompt_template"] = args.prompt_template
        changes["prompt_template"] = args.prompt_template
    if args.task_folder:
        hook["task_folder"] = args.task_folder
        changes["task_folder"] = args.task_folder
    if args.auth_mode:
        hook["auth_mode"] = args.auth_mode
        changes["auth_mode"] = args.auth_mode
    if args.hmac_secret:
        hook["hmac_secret"] = args.hmac_secret
        changes["hmac_secret"] = "(set)"
    if args.hmac_header:
        hook["hmac_header"] = args.hmac_header
        changes["hmac_header"] = args.hmac_header
    if args.hmac_algorithm:
        hook["hmac_algorithm"] = args.hmac_algorithm
        changes["hmac_algorithm"] = args.hmac_algorithm
    if args.hmac_encoding:
        hook["hmac_encoding"] = args.hmac_encoding
        changes["hmac_encoding"] = args.hmac_encoding
    if args.hmac_sig_prefix is not None and "--hmac-sig-prefix" in sys.argv:
        hook["hmac_sig_prefix"] = args.hmac_sig_prefix
        changes["hmac_sig_prefix"] = args.hmac_sig_prefix
    if args.hmac_sig_regex is not None and "--hmac-sig-regex" in sys.argv:
        hook["hmac_sig_regex"] = args.hmac_sig_regex
        changes["hmac_sig_regex"] = args.hmac_sig_regex
    if args.hmac_payload_prefix_regex is not None and "--hmac-payload-prefix-regex" in sys.argv:
        hook["hmac_payload_prefix_regex"] = args.hmac_payload_prefix_regex
        changes["hmac_payload_prefix_regex"] = args.hmac_payload_prefix_regex
    if args.regenerate_token:
        current_mode = hook.get("auth_mode", "bearer")
        if current_mode != "bearer":
            print(
                json.dumps(
                    {
                        "error": "Cannot regenerate token: hook uses HMAC auth, not bearer.",
                        "hint": "HMAC hooks use external signing secrets, not bearer tokens.",
                    }
                )
            )
            sys.exit(1)
        new_token = secrets.token_urlsafe(32)
        hook["token"] = new_token
        changes["token_regenerated"] = True
        changes["new_bearer_token"] = new_token
    if args.provider:
        hook["provider"] = args.provider
        changes["provider"] = args.provider
    if args.model:
        hook["model"] = args.model
        changes["model"] = args.model
    if args.reasoning_effort:
        hook["reasoning_effort"] = args.reasoning_effort
        changes["reasoning_effort"] = args.reasoning_effort
    if args.cli_parameters:
        try:
            cli_params = json.loads(args.cli_parameters)
            if not isinstance(cli_params, list):
                print(json.dumps({"error": "--cli-parameters must be a JSON array"}))
                sys.exit(1)
            hook["cli_parameters"] = cli_params
            changes["cli_parameters"] = cli_params
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid --cli-parameters JSON: {e}"}))
            sys.exit(1)
    if args.quiet_start is not None:
        hook["quiet_start"] = args.quiet_start
        changes["quiet_start"] = args.quiet_start
    if args.quiet_end is not None:
        hook["quiet_end"] = args.quiet_end
        changes["quiet_end"] = args.quiet_end
    if args.dependency is not None:
        hook["dependency"] = args.dependency.strip()
        changes["dependency"] = args.dependency.strip()
    if args.clear_quiet_hours:
        hook.pop("quiet_start", None)
        hook.pop("quiet_end", None)
        changes["quiet_hours"] = "cleared"
    if args.clear_dependency:
        hook.pop("dependency", None)
        changes["dependency"] = "cleared"

    if not changes:
        print(json.dumps({"error": "No changes specified. Use --help for options."}))
        sys.exit(1)

    save_hooks(HOOKS_PATH, data)

    result: dict[str, object] = {
        "hook_id": args.hook_id,
        "changes": changes,
        "updated": True,
    }

    if args.regenerate_token:
        result["action_required"] = (
            "The Bearer token has changed. Update it in all external services "
            "that call this webhook. The old token is now invalid."
        )

    print(json.dumps(result))


if __name__ == "__main__":
    main()
