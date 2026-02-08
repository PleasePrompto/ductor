#!/usr/bin/env python3
"""Send a test payload to a local webhook endpoint.

Automatically uses the hook's auth mode (bearer token or HMAC signature).

Usage:
    python tools/webhook_tools/webhook_test.py "email-notify" \
        --payload '{"from": "user@example.com", "subject": "Hello"}'
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sys
import time
import urllib.request

from _shared import (
    HOOKS_PATH,
    available_hook_ids,
    find_hook,
    load_hooks_or_default,
    load_webhook_config,
)

_TUTORIAL = """\
WEBHOOK TEST -- Send a test payload to your local webhook server.

USAGE:
  python tools/webhook_tools/webhook_test.py "<hook-id>" --payload '<json>'

EXAMPLE:
  python tools/webhook_tools/webhook_test.py "email-notify" \\
      --payload '{"from": "user@example.com", "subject": "Hello"}'

REQUIREMENTS:
  - Webhooks must be enabled in config.json
  - The webhook server must be running (bot must be started)

Auth is resolved automatically from the hook's configuration:
  - bearer mode: uses the hook's per-hook token (or global fallback)
  - hmac mode: signs the payload body with the hook's HMAC secret
"""


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print(_TUTORIAL)
        sys.exit(1)

    hook_id = sys.argv[1].strip()

    payload_str = "{}"
    if "--payload" in sys.argv:
        idx = sys.argv.index("--payload")
        if idx + 1 < len(sys.argv):
            payload_str = sys.argv[idx + 1]

    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        print(json.dumps({"error": "Invalid JSON payload"}))
        sys.exit(1)

    config = load_webhook_config()
    if not config.get("enabled"):
        print(json.dumps({"error": "Webhooks not enabled in config.json"}))
        sys.exit(1)

    # Look up hook for per-hook auth
    hooks_data = load_hooks_or_default(HOOKS_PATH)
    hook = find_hook(hooks_data.get("hooks", []), hook_id)
    if hook is None:
        print(
            json.dumps(
                {
                    "error": f"Hook '{hook_id}' not found in webhooks.json",
                    "available_hooks": available_hook_ids(hooks_data.get("hooks", [])),
                }
            )
        )
        sys.exit(1)

    host = config.get("host", "127.0.0.1")
    port = config.get("port", 8742)
    url = f"http://{host}:{port}/hooks/{hook_id}"
    body_bytes = json.dumps(payload).encode()

    # Build auth headers based on hook's auth_mode
    auth_mode = hook.get("auth_mode", "bearer")
    headers: dict[str, str] = {"Content-Type": "application/json"}

    if auth_mode == "hmac":
        secret = hook.get("hmac_secret", "")
        header_name = hook.get("hmac_header", "")
        if not secret or not header_name:
            print(
                json.dumps(
                    {
                        "error": "HMAC hook is missing hmac_secret or hmac_header",
                        "hint": "Edit the hook to set these values.",
                    }
                )
            )
            sys.exit(1)

        algorithm = hook.get("hmac_algorithm", "sha256")
        encoding = hook.get("hmac_encoding", "hex")
        sig_prefix = hook.get("hmac_sig_prefix", "sha256=")
        sig_regex = hook.get("hmac_sig_regex", "")
        payload_prefix_regex = hook.get("hmac_payload_prefix_regex", "")

        # Build the signed payload (may include timestamp prefix)
        signed_payload = body_bytes
        timestamp_part = ""
        if payload_prefix_regex:
            # Generate a current timestamp for testing
            timestamp_part = str(int(time.time()))
            signed_payload = timestamp_part.encode() + b"." + body_bytes

        computed = hmac.new(secret.encode(), signed_payload, algorithm)
        if encoding == "base64":
            sig_value = base64.b64encode(computed.digest()).decode()
        else:
            sig_value = computed.hexdigest()

        # Build header value matching the hook's expected format
        if sig_regex and payload_prefix_regex and timestamp_part:
            # Reconstruct the header format (e.g. Stripe: "t=123,v1=hex")
            # Extract key names from regex patterns to build header
            ts_key_match = re.match(r"(\w+)=", payload_prefix_regex.replace("\\", ""))
            sig_key_match = re.match(r"(\w+)=", sig_regex.replace("\\", ""))
            ts_key = ts_key_match.group(1) if ts_key_match else "t"
            sig_key = sig_key_match.group(1) if sig_key_match else "v1"
            header_val = f"{ts_key}={timestamp_part},{sig_key}={sig_value}"
        elif sig_prefix:
            header_val = f"{sig_prefix}{sig_value}"
        else:
            header_val = sig_value

        headers[header_name] = header_val
    else:
        token = hook.get("token", "") or config.get("token", "")
        if not token:
            print(json.dumps({"error": "No token configured (neither per-hook nor global)"}))
            sys.exit(1)
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = resp.read().decode()
            print(
                json.dumps(
                    {
                        "status": resp.status,
                        "response": json.loads(resp_body) if resp_body else {},
                        "url": url,
                        "auth_mode": auth_mode,
                    }
                )
            )
    except urllib.error.HTTPError as exc:
        resp_body = exc.read().decode() if exc.fp else ""
        print(
            json.dumps(
                {
                    "status": exc.code,
                    "error": resp_body,
                    "url": url,
                    "auth_mode": auth_mode,
                }
            )
        )
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(
            json.dumps(
                {
                    "error": f"Connection failed: {exc.reason}",
                    "url": url,
                    "hint": "Is the bot running with webhooks enabled?",
                }
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
