#!/usr/bin/env python3
"""Rotate webhook bearer tokens.

Generates new random tokens for one or all hooks with auth_mode="bearer".
HMAC hooks are skipped (they use external signing secrets, not bearer tokens).

Usage:
    python tools/webhook_tools/webhook_rotate_token.py                # All bearer hooks
    python tools/webhook_tools/webhook_rotate_token.py "hook-id"      # Single hook
"""

from __future__ import annotations

import json
import secrets
import sys

from _shared import HOOKS_PATH, available_hook_ids, find_hook, load_hooks_strict, save_hooks

_TUTORIAL = """\
WEBHOOK TOKEN ROTATION -- Generate new Bearer tokens for webhook hooks.

USAGE:
  python tools/webhook_tools/webhook_rotate_token.py                # Rotate ALL bearer hooks
  python tools/webhook_tools/webhook_rotate_token.py "<hook-id>"    # Rotate single hook

WHAT HAPPENS:
  1. A new random token is generated (256-bit, URL-safe).
  2. The old token is immediately invalid.
  3. All external services calling the rotated hook(s) must be updated.

HMAC hooks are skipped because they use external signing secrets, not bearer tokens.

EXAMPLES:
  python tools/webhook_tools/webhook_rotate_token.py                    # All
  python tools/webhook_tools/webhook_rotate_token.py "email-notify"     # Single
"""


def main() -> None:
    hook_id = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else None

    if not HOOKS_PATH.exists():
        print(json.dumps({"error": "No webhooks.json file found"}))
        sys.exit(1)

    try:
        data = load_hooks_strict(HOOKS_PATH)
    except (json.JSONDecodeError, TypeError):
        print(json.dumps({"error": "Corrupt webhooks.json -- cannot parse"}))
        sys.exit(1)

    hooks = data.get("hooks", [])

    if hook_id:
        hook = find_hook(hooks, hook_id)
        if hook is None:
            print(
                json.dumps(
                    {
                        "error": f"Hook '{hook_id}' not found",
                        "available_hooks": available_hook_ids(hooks),
                    }
                )
            )
            sys.exit(1)
        if hook.get("auth_mode", "bearer") != "bearer":
            print(
                json.dumps(
                    {
                        "error": f"Hook '{hook_id}' uses auth_mode='{hook.get('auth_mode')}', not bearer.",
                        "hint": "HMAC hooks use external signing secrets. Rotate them on the external service.",
                    }
                )
            )
            sys.exit(1)

    rotated = []
    for h in hooks:
        if h.get("auth_mode", "bearer") != "bearer":
            continue
        if hook_id and h.get("id") != hook_id:
            continue

        new_token = secrets.token_urlsafe(32)
        h["token"] = new_token
        rotated.append(
            {
                "hook_id": h["id"],
                "title": h.get("title", ""),
                "new_bearer_token": new_token,
            }
        )

    if not rotated:
        print(
            json.dumps(
                {
                    "error": "No bearer hooks found to rotate",
                    "hint": "Only hooks with auth_mode='bearer' can have tokens rotated.",
                }
            )
        )
        sys.exit(1)

    save_hooks(HOOKS_PATH, data)

    print(
        json.dumps(
            {
                "rotated": rotated,
                "count": len(rotated),
                "action_required": [
                    "Update the Bearer token in ALL external services that call these webhooks.",
                    "The old tokens are now INVALID. Requests with old tokens will be rejected (401).",
                    "Each hook above shows its new token under 'new_bearer_token'.",
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
