#!/usr/bin/env python3
"""Remove a webhook: deletes the JSON entry only.

Does NOT delete cron_tasks/ folders (they may be shared with cron jobs).

Usage:
    python tools/webhook_tools/webhook_remove.py "email-notify"
"""

from __future__ import annotations

import json
import sys

from _shared import HOOKS_PATH, available_hook_ids, load_hooks_strict, save_hooks

_TUTORIAL = """\
WEBHOOK REMOVE -- Delete a registered webhook endpoint.

This tool removes the hook entry from webhooks.json.
It does NOT delete cron_tasks/ folders (they may be shared with cron jobs).

USAGE:
  python tools/webhook_tools/webhook_remove.py "<hook-id>"

EXAMPLE:
  python tools/webhook_tools/webhook_remove.py "email-notify"

IMPORTANT:
  Use the EXACT hook ID from webhooks.json. Run webhook_list.py first.
"""


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print(_TUTORIAL)
        sys.exit(1)

    hook_id = sys.argv[1].strip()

    if not HOOKS_PATH.exists():
        print(
            json.dumps(
                {"error": f"Hook '{hook_id}' not found (no webhooks.json)", "available_hooks": []}
            )
        )
        sys.exit(1)

    try:
        data = load_hooks_strict(HOOKS_PATH)
    except (json.JSONDecodeError, TypeError):
        print(json.dumps({"error": "Corrupt webhooks.json -- cannot parse"}))
        sys.exit(1)

    hooks = data.get("hooks", [])
    hook = next((h for h in hooks if h.get("id") == hook_id), None)

    if hook is None:
        available = available_hook_ids(hooks)
        print(
            json.dumps(
                {
                    "error": f"Hook '{hook_id}' not found",
                    "hint": "Use the EXACT hook ID from webhook_list.py output.",
                    "available_hooks": available,
                }
            )
        )
        sys.exit(1)

    data["hooks"] = [h for h in hooks if h.get("id") != hook_id]
    save_hooks(HOOKS_PATH, data)

    result = {
        "hook_id": hook_id,
        "json_entry_removed": True,
        "note": "cron_tasks/ folder was NOT deleted (may be shared with cron jobs).",
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
