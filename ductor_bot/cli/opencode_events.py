"""NDJSON event parsers for the opencode CLI (``opencode run --format json``).

``opencode run <message> --format json`` streams one JSON object per line::

    {"type": "step_start", "timestamp": ..., "sessionID": "ses_...", "part": {...}}
    {
        "type": "text",
        "timestamp": ...,
        "sessionID": "ses_...",
        "part": {"type": "text", "text": "..."},
    }
    {
        "type": "tool_use",
        "timestamp": ...,
        "sessionID": "ses_...",
        "part": {"type": "tool", "tool": "bash", "state": {...}},
    }
    {
        "type": "step_finish",
        "timestamp": ...,
        "sessionID": "ses_...",
        "part": {"type": "step-finish", "reason": "stop", "tokens": {...}},
    }
    {
        "type": "error",
        "timestamp": ...,
        "sessionID": "ses_...",
        "error": {"name": "...", "data": {"message": "..."}},
    }

Text parts are only emitted once complete, so every ``text`` event carries a
full chunk. The stream ends when the session goes idle; there is no explicit
``end`` marker, so callers must synthesize the final result event.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
    ThinkingEvent,
    ToolUseEvent,
)

logger = logging.getLogger(__name__)

_USAGE_KEYS = ("input", "output", "total")


def parse_opencode_json(
    raw: str,
) -> tuple[str, str | None, dict[str, Any], dict[str, Any], int | None, bool, float | None]:
    """Parse oneshot ``opencode run --format json`` NDJSON output.

    Returns:
        (text, session_id, usage, model_usage, num_turns, is_error, total_cost_usd)
    """
    stripped = raw.strip()
    if not stripped:
        return "", None, {}, {}, None, True, None

    state: dict[str, Any] = {
        "text_parts": [],
        "session_id": None,
        "usage": {},
        "total_cost": None,
        "num_turns": 0,
        "is_error": False,
        "parsed_any": False,
    }

    for line in stripped.splitlines():
        try:
            data: Any = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("OpenCode: unparseable JSON line: %.200s", line)
            continue
        if not isinstance(data, dict):
            continue
        state["parsed_any"] = True
        _accumulate_event(data, state)

    if not state["parsed_any"]:
        # Nothing looked like JSON -- surface the raw output as plain text.
        return stripped, None, {}, {}, None, False, None

    num_turns_out = state["num_turns"] or None
    return (
        "\n".join(state["text_parts"]).strip(),
        state["session_id"],
        state["usage"],
        {},
        num_turns_out,
        state["is_error"],
        state["total_cost"],
    )


def _accumulate_event(data: dict[str, Any], state: dict[str, Any]) -> None:
    """Fold one NDJSON event line into the oneshot aggregation *state*."""
    if data.get("sessionID") and not state["session_id"]:
        state["session_id"] = str(data["sessionID"])
    handler = _ONESHOOT_HANDLERS.get(str(data.get("type", "")))
    if handler is not None:
        handler(data, state)


def _acc_text(data: dict[str, Any], state: dict[str, Any]) -> None:
    part = data.get("part")
    if isinstance(part, dict) and part.get("type") == "text":
        text = part.get("text")
        if text:
            state["text_parts"].append(_as_str(text))


def _acc_step_start(_data: dict[str, Any], state: dict[str, Any]) -> None:
    state["num_turns"] += 1


def _acc_step_finish(data: dict[str, Any], state: dict[str, Any]) -> None:
    part = data.get("part")
    if not isinstance(part, dict):
        return
    usage, step_cost = _extract_usage(state["usage"], part.get("tokens"))
    state["usage"] = usage
    if step_cost is None:
        # opencode reports cost at the part level (sibling of tokens).
        raw_cost = part.get("cost", part.get("costUSD"))
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
            step_cost = float(raw_cost)
    if step_cost is not None:
        state["total_cost"] = (state["total_cost"] or 0.0) + step_cost


def _acc_error(data: dict[str, Any], state: dict[str, Any]) -> None:
    state["is_error"] = True
    message = _error_message(data)
    if message:
        state["text_parts"].append(message)


_Accumulator = Callable[[dict[str, Any], dict[str, Any]], None]

_ONESHOOT_HANDLERS: dict[str, _Accumulator] = {
    "text": _acc_text,
    "step_start": _acc_step_start,
    "step_finish": _acc_step_finish,
    "error": _acc_error,
}


def parse_opencode_stream_line(line: str) -> list[StreamEvent]:
    """Parse a single ``opencode run --format json`` line into stream events."""
    stripped = line.strip()
    if not stripped:
        return []

    try:
        data: Any = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("OpenCode: unparseable stream line: %.200s", stripped)
        return []

    if not isinstance(data, dict):
        return []

    event_type = str(data.get("type", ""))
    handler = _STREAM_HANDLERS.get(event_type)
    if handler is None:
        logger.debug("OpenCode: ignoring stream event type=%s", event_type)
        return []
    return handler(event_type, data)


def extract_opencode_session_id(line: str) -> str | None:
    """Return the session id carried by a raw event line, if any."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and data.get("sessionID"):
        return str(data["sessionID"])
    return None


def _parse_text(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    part = data.get("part")
    if not isinstance(part, dict):
        return []
    text = part.get("text")
    return [AssistantTextDelta(type="assistant", text=_as_str(text))] if text else []


def _parse_reasoning(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    part = data.get("part")
    if not isinstance(part, dict):
        return []
    text = part.get("text")
    return [ThinkingEvent(type="assistant", text=_as_str(text))] if text else []


def _parse_tool_use(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    part = data.get("part")
    if not isinstance(part, dict):
        return []
    name = _as_str(part.get("tool") or part.get("name") or "tool")
    tool_id = part.get("id")
    state = part.get("state")
    params = state.get("input") if isinstance(state, dict) else None
    if not isinstance(params, dict):
        params = None
    return [
        ToolUseEvent(
            type="assistant", tool_name=name, tool_id=_as_str(tool_id) or None, parameters=params
        )
    ]


def _parse_error(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    """Failure path: emit a terminal ResultEvent so the stream ends cleanly."""
    message = _error_message(data)
    session_id = data.get("sessionID")
    return [
        ResultEvent(
            type="result",
            result=message,
            is_error=True,
            session_id=_as_str(session_id) or None,
        )
    ]


_StreamHandler = Callable[[str, dict[str, Any]], list[StreamEvent]]

_STREAM_HANDLERS: dict[str, _StreamHandler] = {
    "text": _parse_text,
    "reasoning": _parse_reasoning,
    "tool_use": _parse_tool_use,
    "error": _parse_error,
}


def _extract_usage(
    acc: dict[str, Any],
    tokens: Any,
) -> tuple[dict[str, Any], float | None]:
    """Merge opencode step-finish token counters into a usage dict.

    opencode token payloads vary by version; the shared counters are
    ``input`` / ``output`` / ``total`` plus a cost value (``cost`` or
    ``costUSD``). Every value is best-effort.
    """
    if not isinstance(tokens, dict):
        return acc, None
    merged = dict(acc)
    for key in _USAGE_KEYS:
        raw = tokens.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            merged[f"{key}_tokens"] = merged.get(f"{key}_tokens", 0) + int(raw)

    cost: float | None = None
    raw_cost = tokens.get("costUSD", tokens.get("cost"))
    if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
        cost = float(raw_cost)
    elif isinstance(tokens.get("costIn"), (int, float)) and isinstance(
        tokens.get("costOut"), (int, float)
    ):
        cost = float(tokens["costIn"]) + float(tokens["costOut"])
    return merged, cost


def _error_message(data: dict[str, Any]) -> str:
    """Extract a human-readable message from an opencode error event."""
    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("data")
        if isinstance(message, dict) and message.get("message"):
            return _as_str(message["message"])
        message = error.get("message")
        if message:
            return _as_str(message)
        return _as_str(error.get("name") or error)
    if isinstance(error, str) and error:
        return error
    return _as_str(data.get("message") or "")


def _as_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
