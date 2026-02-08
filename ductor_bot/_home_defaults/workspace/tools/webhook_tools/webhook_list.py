#!/usr/bin/env python3
"""List all registered webhooks with status and error info.

Usage:
    python tools/webhook_tools/webhook_list.py
"""

from __future__ import annotations

import json

from _shared import CRON_TASKS_DIR, HOOKS_PATH, load_hooks_or_default, load_webhook_config


def main() -> None:
    data = load_hooks_or_default(HOOKS_PATH)
    config = load_webhook_config()

    hooks = []
    for h in data.get("hooks", []):
        task_folder = h.get("task_folder", "")
        auth_mode = h.get("auth_mode", "bearer")
        entry: dict = {
            "id": h["id"],
            "title": h.get("title", ""),
            "mode": h.get("mode", ""),
            "auth_mode": auth_mode,
            "endpoint": f"/hooks/{h['id']}",
            "enabled": h.get("enabled", True),
            "token_set": bool(h.get("token", "")),
            "hmac_configured": bool(h.get("hmac_secret", "")),
            "hmac_algorithm": h.get("hmac_algorithm", "sha256") if auth_mode == "hmac" else None,
            "hmac_encoding": h.get("hmac_encoding", "hex") if auth_mode == "hmac" else None,
            "trigger_count": h.get("trigger_count", 0),
            "last_triggered_at": h.get("last_triggered_at"),
            "last_error": h.get("last_error"),
            "prompt_template": h.get("prompt_template", "")[:80],
        }
        if task_folder:
            entry["task_folder"] = task_folder
            entry["task_folder_exists"] = (CRON_TASKS_DIR / task_folder).is_dir()
        hooks.append(entry)

    server_info = {
        "enabled": config.get("enabled", False),
        "host": config.get("host", "127.0.0.1"),
        "port": config.get("port", 8742),
        "global_token_set": bool(config.get("token", "")),
    }

    print(
        json.dumps(
            {
                "hooks": hooks,
                "count": len(hooks),
                "server": server_info,
                "how_to_create": (
                    "python tools/webhook_tools/webhook_add.py "
                    '--name "..." --title "..." --description "..." '
                    '--mode "wake" --prompt-template "..."'
                ),
                "how_to_expose": "cloudflared tunnel --url http://localhost:8742",
                "how_to_rotate_tokens": (
                    "python tools/webhook_tools/webhook_rotate_token.py             # all bearer hooks\n"
                    'python tools/webhook_tools/webhook_rotate_token.py "hook-id"   # single hook'
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
