"""Recipient-side Wake SLO responder.

This module short-circuits AgentComm wake-push payloads that match the
Wake SLO probe protocol, so they get an ack + pong reply via the local
Qoopia MCP endpoint **without** spawning a Claude session. The point is
to measure the wake-push transport and the recipient-side dispatch
machinery, not the LLM session liveness.

It is the recipient-side counterpart to the sender-side probe at
``/srv/qoopia/code/scripts/wake_slo_probe.ts``. A sender-side responder
would synthesize the pong locally and fake L2/L3/L4 proof — see
:doc:`wake-slo-responder-contract.md` for the architecture rationale.

Auth: reads the recipient agent's Qoopia API key from a file path
configured via the ``QOOPIA_API_KEY_FILE`` environment variable, falling
back to ``~/.ductor-corsairmain/.secrets/qoopia_api_key``. The MCP
endpoint defaults to ``http://127.0.0.1:3738/mcp`` and is overridable via
``QOOPIA_PUBLIC_URL``.

Network: stdlib ``urllib.request`` only — no extra dependencies. The
Qoopia MCP server is configured in stateless mode (no session-id), so a
single JSON-RPC POST per call is enough.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = logging.getLogger(__name__)

TOPIC_RE = re.compile(r"^WAKE_SLO_PROBE_(C2L|L2C)_\d+$")
BODY_RE = re.compile(
    r"^WAKE_SLO_PING (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)$"
)

DEFAULT_API_KEY_FILE = "~/.ductor-corsairmain/.secrets/qoopia_api_key"
DEFAULT_MCP_URL = "http://127.0.0.1:3738/mcp"
MCP_TIMEOUT_SECONDS = 5.0


def is_probe_payload(payload: dict[str, Any]) -> bool:
    """Return True iff this webhook payload is a Wake SLO probe."""
    if not isinstance(payload, dict):
        return False
    if payload.get("event_type") != "agentcomm_wake":
        return False
    topic = payload.get("topic")
    body = payload.get("body")
    if not isinstance(topic, str) or not isinstance(body, str):
        return False
    return bool(TOPIC_RE.match(topic)) and bool(BODY_RE.match(body))


def _read_api_key() -> str | None:
    # Resolution order:
    #   1. WAKE_SLO_RESPONDER_API_KEY env var (raw value) — explicit override
    #      so deployments can name the key the responder uses without
    #      hijacking other env vars.
    #   2. WAKE_SLO_RESPONDER_API_KEY_FILE env var (path to a key file).
    #   3. Default file path (steward-style key, may not have ack rights
    #      on messages addressed to the local recipient agent — see the
    #      contract doc note on per-agent key sourcing).
    env_value = os.environ.get("WAKE_SLO_RESPONDER_API_KEY")
    if env_value:
        return env_value.strip() or None
    path_str = os.environ.get(
        "WAKE_SLO_RESPONDER_API_KEY_FILE",
        os.environ.get("QOOPIA_API_KEY_FILE", DEFAULT_API_KEY_FILE),
    )
    try:
        return Path(path_str).expanduser().read_text(encoding="utf-8").strip() or None
    except OSError as e:
        logger.warning("wake_slo_responder: api_key unreadable at %s: %s", path_str, e)
        return None


def _mcp_url() -> str:
    base = os.environ.get("QOOPIA_PUBLIC_URL", "").rstrip("/")
    return f"{base}/mcp" if base else DEFAULT_MCP_URL


def _mcp_call(method: str, name: str, arguments: dict[str, Any], api_key: str) -> dict[str, Any]:
    """One JSON-RPC POST to Qoopia MCP (stateless). Returns parsed result.

    The endpoint emits SSE-framed ``event: message\\ndata: {json}\\n``.
    We strip the framing and JSON-parse the data line.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {"name": name, "arguments": arguments},
    }
    req = urllib_request.Request(
        _mcp_url(),
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib_request.urlopen(req, timeout=MCP_TIMEOUT_SECONDS) as resp:
        raw = resp.read().decode("utf-8")
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    return json.loads(raw)


def respond(payload: dict[str, Any]) -> dict[str, Any]:
    """Ack and pong-reply this probe via Qoopia MCP.

    Returns a dict suitable for logging. Always returns; never raises out
    to the caller. On error, ``status`` will be a string starting with
    ``error:`` so the dispatch can fall through to the normal Claude
    session as a safety net.
    """
    api_key = _read_api_key()
    if not api_key:
        return {"status": "error:no_api_key"}

    body = payload.get("body", "")
    m = BODY_RE.match(body)
    if not m:
        return {"status": "error:body_regex_mismatch"}
    pong_ts = m.group(1)

    message_id = payload.get("message_id")
    session_id = payload.get("session_id")
    if not isinstance(message_id, str) or not isinstance(session_id, str):
        return {"status": "error:missing_ids"}

    try:
        ack_resp = _mcp_call(
            "tools/call",
            "agent_ack",
            {"message_id": message_id, "status": "auto_pong"},
            api_key,
        )
        if "error" in ack_resp:
            return {"status": "error:ack_failed", "detail": ack_resp["error"]}

        reply_resp = _mcp_call(
            "tools/call",
            "agent_reply",
            {
                "session_id": session_id,
                "body": f"WAKE_SLO_PONG {pong_ts}",
                "close": True,
            },
            api_key,
        )
        if "error" in reply_resp:
            return {"status": "error:reply_failed", "detail": reply_resp["error"]}
    except urllib_error.URLError as e:
        return {"status": "error:network", "detail": str(e)}
    except Exception as e:  # noqa: BLE001 — never raise out of the responder
        return {"status": "error:exception", "detail": str(e)}

    return {
        "status": "ok",
        "message_id": message_id,
        "session_id": session_id,
        "pong_ts": pong_ts,
    }
