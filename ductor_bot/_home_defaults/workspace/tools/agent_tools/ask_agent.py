#!/usr/bin/env python3
"""Send a synchronous message to another agent via the InterAgentBus.

Blocks until the sub-agent responds. The response is returned as stdout
to the calling CLI process (your tool output).

The response ALWAYS comes back to YOU (the calling agent). There is no way
to make the sub-agent reply in its own Telegram chat via this tool.

Uses the internal localhost HTTP API to communicate with the bus.
Environment variables DUCTOR_AGENT_NAME, DUCTOR_INTERAGENT_PORT, and
DUCTOR_INTERAGENT_HOST are automatically set by the Ductor framework.

Usage:
    python3 ask_agent.py [--new] TARGET_AGENT "Your message here"

Options:
    --new   Start a fresh session, discarding any prior inter-agent context
            with the recipient.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error


def main() -> None:
    args = sys.argv[1:]
    new_session = False
    if args and args[0] == "--new":
        new_session = True
        args = args[1:]

    if len(args) < 2:
        print('Usage: python3 ask_agent.py [--new] TARGET_AGENT "message"', file=sys.stderr)
        sys.exit(1)

    target = args[0]
    message = args[1]
    port = os.environ.get("DUCTOR_INTERAGENT_PORT", "8799")
    host = os.environ.get("DUCTOR_INTERAGENT_HOST", "127.0.0.1")
    sender = os.environ.get("DUCTOR_AGENT_NAME", "unknown")

    url = f"http://{host}:{port}/interagent/send"
    body: dict[str, object] = {"from": sender, "to": target, "message": message}
    if new_session:
        body["new_session"] = True
    chat_id = os.environ.get("DUCTOR_CHAT_ID", "")
    topic_id = os.environ.get("DUCTOR_TOPIC_ID", "")
    if chat_id:
        body["chat_id"] = int(chat_id)
    if topic_id:
        body["topic_id"] = int(topic_id)
    payload = json.dumps(body).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        print(f"Error: Cannot reach inter-agent API at {url}: {e}", file=sys.stderr)
        print(
            "Make sure the Ductor supervisor is running with multi-agent support.", file=sys.stderr
        )
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if result.get("success"):
        print(result.get("text", ""))
    else:
        error = result.get("error", "Unknown error")
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
