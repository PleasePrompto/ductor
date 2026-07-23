"""JSON / streaming-json parsers for the xAI Grok Build CLI (`grok`).

Headless JSON (``--output-format json``) returns one object::

    {
        "text": "...",
        "stopReason": "EndTurn",
        "sessionId": "...",
        "usage": {...},
        "num_turns": 1,
        "modelUsage": {...},
        "total_cost_usd": 0.01,
    }

Streaming JSON (``--output-format streaming-json``) is NDJSON::

    {"type":"thought","data":"..."}
    {"type":"text","data":"..."}
    {"type":"error","message":"..."}
    {"type":"auto_compact_start", ...}
    {"type":"end","stopReason":"...","sessionId":"...","usage":{...}, ...}

Optional tool events (when present) use::

    {"type": "tool_use" | "tool", "name": "...", "id": "...", "input": {...}}
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    CompactBoundaryEvent,
    ResultEvent,
    StreamEvent,
    SystemStatusEvent,
    ThinkingEvent,
    ToolUseEvent,
)

logger = logging.getLogger(__name__)

# stopReason values treated as successful completion.
_OK_STOP_REASONS = frozenset({"EndTurn", "end_turn", "stop", "Stop", "completed", "Completed"})


def parse_grok_json(
    raw: str,
) -> tuple[str, str | None, dict[str, Any], dict[str, Any], int | None, bool, float | None]:
    """Parse oneshot Grok JSON.

    Returns:
        (text, session_id, usage, model_usage, num_turns, is_error, total_cost_usd)
    """
    stripped = raw.strip()
    if not stripped:
        return "", None, {}, {}, None, True, None

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("Grok: unparseable JSON envelope, treating as plain text")
        return stripped, None, {}, {}, None, False, None

    if not isinstance(data, dict):
        return str(data), None, {}, {}, None, False, None

    # Streaming-style error envelope leaked into oneshot stdout.
    if str(data.get("type", "")).lower() == "error":
        msg = _as_str(data.get("message") or data.get("error") or data.get("text") or raw)
        usage_err, model_usage_err, cost_err = _extract_spend(data)
        return msg, None, usage_err, model_usage_err, None, True, cost_err

    text = _as_str(data.get("text") or data.get("result") or data.get("content") or "")
    session_id = _as_str(data.get("sessionId") or data.get("session_id") or "") or None
    usage, model_usage, total_cost = _extract_spend(data)
    num_turns = data.get("num_turns")
    if not isinstance(num_turns, int):
        num_turns = None
    is_error = _is_error_payload(data)
    return text, session_id, usage, model_usage, num_turns, is_error, total_cost


def parse_grok_stream_line(line: str) -> list[StreamEvent]:
    """Parse a single Grok streaming-json NDJSON line into stream events."""
    stripped = line.strip()
    if not stripped:
        return []

    try:
        payload: Any = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("Grok: unparseable stream line: %.200s", stripped)
        return []

    if not isinstance(payload, dict):
        return []
    data: dict[str, Any] = payload

    event_type = str(data.get("type", "")).lower()
    handler = _stream_handler_for(event_type)
    if handler is None:
        logger.debug("Grok: ignoring stream event type=%s", event_type)
        return []
    return handler(event_type, data)


def _parse_thinking(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    text = _as_str(data.get("data") or data.get("text") or data.get("content") or "")
    return [ThinkingEvent(type="assistant", text=text)] if text else []


def _parse_text(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    text = _as_str(data.get("data") or data.get("text") or data.get("content") or "")
    return [AssistantTextDelta(type="assistant", text=text)] if text else []


def _parse_tool_use(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    name = _as_str(data.get("name") or data.get("tool_name") or data.get("tool") or "tool")
    tool_id = _as_str(data.get("id") or data.get("tool_id") or "") or None
    params = data.get("input") or data.get("parameters") or data.get("arguments")
    if not isinstance(params, dict):
        params = None
    return [ToolUseEvent(type="assistant", tool_name=name, tool_id=tool_id, parameters=params)]


def _parse_error(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    """Failure path: emit a terminal ResultEvent so the stream ends cleanly."""
    usage_err, model_usage_err, cost_err = _extract_spend(data)
    msg = _as_str(data.get("message") or data.get("error") or data.get("data") or "Grok error")
    session_id = _as_str(data.get("sessionId") or data.get("session_id") or "") or None
    return [
        ResultEvent(
            type="result",
            session_id=session_id,
            result=msg,
            is_error=True,
            usage=usage_err,
            model_usage=model_usage_err,
            total_cost_usd=cost_err,
        )
    ]


def _parse_compact(event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    """Compaction boundary → orchestrator memory flush (same as Claude compact_boundary)."""
    pre_tokens = data.get("pre_tokens")
    if not isinstance(pre_tokens, int):
        pre_tokens = data.get("tokens")
    if not isinstance(pre_tokens, int):
        pre_tokens = 0
    return [
        CompactBoundaryEvent(
            type="system",
            subtype="compact_boundary",
            trigger=_as_str(data.get("trigger") or event_type),
            pre_tokens=pre_tokens,
        )
    ]


def _parse_max_turns(_event_type: str, _data: dict[str, Any]) -> list[StreamEvent]:
    return [SystemStatusEvent(type="system", subtype="status", status="max_turns_reached")]


def _parse_end(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    session_id = _as_str(data.get("sessionId") or data.get("session_id") or "") or None
    usage_end, model_usage_end, cost_end = _extract_spend(data)
    result_text = _as_str(data.get("text") or data.get("result") or data.get("data") or "")
    num_turns = data.get("num_turns")
    if not isinstance(num_turns, int):
        num_turns = None
    return [
        ResultEvent(
            type="result",
            session_id=session_id,
            result=result_text,
            is_error=_is_error_payload(data),
            usage=usage_end,
            model_usage=model_usage_end,
            num_turns=num_turns,
            total_cost_usd=cost_end,
        )
    ]


_StreamHandler = Callable[[str, dict[str, Any]], list[StreamEvent]]

_STREAM_HANDLERS: dict[str, _StreamHandler] = {
    **dict.fromkeys(("thought", "thinking", "reasoning"), _parse_thinking),
    **dict.fromkeys(("text", "assistant", "message", "agent_message"), _parse_text),
    **dict.fromkeys(("tool_use", "tool", "tool_call"), _parse_tool_use),
    "error": _parse_error,
    **dict.fromkeys(("compact", "compact_boundary", "compaction"), _parse_compact),
    "max_turns_reached": _parse_max_turns,
    **dict.fromkeys(("end", "result", "done", "final"), _parse_end),
}


def _stream_handler_for(event_type: str) -> _StreamHandler | None:
    if event_type.startswith("auto_compact"):
        return _parse_compact
    return _STREAM_HANDLERS.get(event_type)


def _extract_spend(
    data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], float | None]:
    """Pull usage / modelUsage / total_cost_usd from a Grok payload."""
    usage: dict[str, Any] = data["usage"] if isinstance(data.get("usage"), dict) else {}
    model_usage: dict[str, Any] = (
        data["modelUsage"] if isinstance(data.get("modelUsage"), dict) else {}
    )
    if not model_usage and isinstance(data.get("model_usage"), dict):
        model_usage = data["model_usage"]

    total_cost: float | None = None
    raw_cost = data.get("total_cost_usd")
    if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
        total_cost = float(raw_cost)
    elif not usage and model_usage:
        # Sum partial per-model costs when top-level cost is absent but rows exist.
        summed = 0.0
        saw = False
        for row in model_usage.values():
            if isinstance(row, dict) and isinstance(row.get("costUSD"), (int, float)):
                summed += float(row["costUSD"])
                saw = True
        if saw:
            total_cost = summed

    return usage, model_usage, total_cost


def _is_error_payload(data: dict[str, Any]) -> bool:
    if data.get("is_error") is True or data.get("isError") is True:
        return True
    if data.get("error"):
        return True
    stop = data.get("stopReason") or data.get("stop_reason") or ""
    if isinstance(stop, str) and stop and stop not in _OK_STOP_REASONS:
        # Refusal / cancelled / error style stop reasons.
        lowered = stop.lower()
        if any(tok in lowered for tok in ("error", "fail", "cancel", "refus", "abort")):
            return True
    return False


def _as_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
