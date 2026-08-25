"""JSON / streaming-json parsers for the Oh My Pi CLI (``omp``)."""

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
    SystemInitEvent,
    ThinkingEvent,
    ToolUseEvent,
)

logger = logging.getLogger(__name__)


def parse_omp_json(  # noqa: C901, PLR0912, PLR0915
    raw: str,
) -> tuple[str, str | None, dict[str, Any], dict[str, Any], int | None, bool, float | None]:
    """Parse oneshot ``omp --mode=json`` output.

    The non-streaming JSON is NDJSON; the interesting record is the
    ``agent_end`` event whose ``messages`` array holds the history.
    Falls back to plain-text when no envelope is found.

    Returns:
        (text, session_id, usage, model_usage, num_turns, is_error, total_cost_usd)
    """
    stripped = raw.strip()
    if not stripped:
        return "", None, {}, {}, None, True, None

    session_id: str | None = None
    last_text = ""
    usage: dict[str, Any] = {}
    model_usage: dict[str, Any] = {}
    total_cost: float | None = None
    is_error = False

    found_envelope = False
    for raw_line in stripped.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") == "session" and isinstance(data.get("id"), str):
            session_id = str(data["id"])
            found_envelope = True
            continue
        if data.get("type") == "agent_end":
            found_envelope = True
            sid = data.get("id") if isinstance(data.get("id"), str) else None
            if sid:
                session_id = str(sid)
            messages = data.get("messages")
            if isinstance(messages, list):
                for msg in reversed(messages):
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("role") != "assistant":
                        continue
                    content = msg.get("content")
                    if isinstance(content, list) and content:
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                last_text = _as_str(part.get("text") or "")
                                break
                        if last_text:
                            break
                    elif isinstance(content, str):
                        last_text = content
                        break
                for msg in reversed(messages):
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        u = msg.get("usage")
                        if isinstance(u, dict):
                            usage = dict(u)
                            if "input" in u and "input_tokens" not in usage:
                                usage["input_tokens"] = u["input"]
                            if "output" in u and "output_tokens" not in usage:
                                usage["output_tokens"] = u["output"]
                        c = msg.get("cost")
                        if isinstance(c, dict) and isinstance(c.get("total"), (int, float)):
                            total_cost = float(c["total"])
                        mu = msg.get("modelUsage") or msg.get("model_usage")
                        if isinstance(mu, dict):
                            model_usage = dict(mu)
                        break
            if not usage:
                usage, model_usage, total_cost = _extract_spend(data)
            continue
        if str(data.get("type", "")).lower() == "error":
            found_envelope = True
            msg = _as_str(data.get("message") or data.get("error") or raw)
            usage_err, model_usage_err, cost_err = _extract_spend(data)
            return msg, session_id, usage_err, model_usage_err, None, True, cost_err

    if not found_envelope:
        lower = stripped.lower()
        if any(tok in lower for tok in ("error", "not found", "failed", "api key")):
            return stripped, None, {}, {}, None, True, None
        return stripped, session_id, {}, {}, None, False, None

    return last_text, session_id, usage, model_usage, None, is_error, total_cost


def parse_omp_stream_line(line: str) -> list[StreamEvent]:  # noqa: PLR0911
    """Parse a single ``omp --mode=json`` NDJSON line into stream events."""
    stripped = line.strip()
    if not stripped:
        return []

    try:
        payload: Any = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("Omp: unparseable stream line: %.200s", stripped)
        return []

    if not isinstance(payload, dict):
        return []

    data: dict[str, Any] = payload
    event_type = str(data.get("type", "")).lower()

    if event_type in _TOP_LEVEL_HANDLERS:
        return _TOP_LEVEL_HANDLERS[event_type](event_type, data)

    if event_type == "message_update":
        inner = data.get("assistantMessageEvent")
        if isinstance(inner, dict):
            inner_type = str(inner.get("type", "")).lower()
            handler = _INNER_HANDLERS.get(inner_type)
            if handler is not None:
                return handler(inner_type, inner)
            logger.debug("Omp: ignoring assistantMessageEvent type=%s", inner_type)
        return []

    logger.debug("Omp: ignoring stream event type=%s", event_type)
    return []


def _handle_session(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    session_id = _as_str(data.get("id") or data.get("session_id") or "") or None
    if not session_id:
        return []
    return [SystemInitEvent(type="system", subtype="init", session_id=session_id)]


def _handle_agent_start(_event_type: str, _data: dict[str, Any]) -> list[StreamEvent]:
    return []


def _handle_agent_end(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:  # noqa: C901, PLR0912
    session_id = _as_str(data.get("id") or data.get("session_id") or "") or None
    text = ""
    usage: dict[str, Any] = {}
    model_usage: dict[str, Any] = {}
    total_cost: float | None = None
    is_error = False
    messages = data.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = _as_str(part.get("text") or "")
                        if text:
                            break
                if text:
                    break
            elif isinstance(content, str):
                text = content
                break
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                u = msg.get("usage")
                if isinstance(u, dict):
                    usage = dict(u)
                    if "input" in u and "input_tokens" not in usage:
                        usage["input_tokens"] = u["input"]
                    if "output" in u and "output_tokens" not in usage:
                        usage["output_tokens"] = u["output"]
                c = msg.get("cost")
                if isinstance(c, dict) and isinstance(c.get("total"), (int, float)):
                    total_cost = float(c["total"])
                mu = msg.get("modelUsage") or msg.get("model_usage")
                if isinstance(mu, dict):
                    model_usage = dict(mu)
                break
    if not text:
        fallback = data.get("result") or data.get("text") or ""
        text = _as_str(fallback)
    if not usage:
        usage, model_usage, total_cost = _extract_spend(data)
    if (
        data.get("is_error") is True
        or data.get("isError") is True
        or (isinstance(data.get("error"), str) and data["error"])
    ):
        is_error = True
    return [
        ResultEvent(
            type="result",
            session_id=session_id,
            result=text,
            is_error=is_error,
            usage=usage,
            model_usage=model_usage,
            total_cost_usd=total_cost,
        )
    ]


def _handle_turn_start(_event_type: str, _data: dict[str, Any]) -> list[StreamEvent]:
    return []


def _handle_turn_end(_event_type: str, _data: dict[str, Any]) -> list[StreamEvent]:
    return []


def _handle_message_start(_event_type: str, _data: dict[str, Any]) -> list[StreamEvent]:
    return []


def _handle_message_end(_event_type: str, _data: dict[str, Any]) -> list[StreamEvent]:
    return []


def _handle_tool_execution_start(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    name = _as_str(data.get("toolName") or data.get("tool_name") or data.get("name") or "tool")
    tool_id = _as_str(data.get("toolCallId") or data.get("tool_call_id") or "") or None
    params = data.get("args") if isinstance(data.get("args"), dict) else None
    if params is None and isinstance(data.get("input"), dict):
        params = data["input"]
    return [ToolUseEvent(type="assistant", tool_name=name, tool_id=tool_id, parameters=params)]


def _handle_tool_execution_update(_event_type: str, _data: dict[str, Any]) -> list[StreamEvent]:
    return []


def _handle_tool_execution_end(_event_type: str, _data: dict[str, Any]) -> list[StreamEvent]:
    return []


def _handle_error(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    msg = _as_str(data.get("message") or data.get("error") or data.get("text") or "Oh My Pi error")
    usage, model_usage, cost = _extract_spend(data)
    return [
        ResultEvent(
            type="result",
            result=msg,
            is_error=True,
            usage=usage,
            model_usage=model_usage,
            total_cost_usd=cost,
        )
    ]


def _handle_compact(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    trigger = _as_str(data.get("trigger") or data.get("reason") or "auto")
    pre = data.get("pre_tokens")
    pre_tokens = int(pre) if isinstance(pre, int) else 0
    return [
        CompactBoundaryEvent(
            type="system", subtype="compact_boundary", trigger=trigger, pre_tokens=pre_tokens
        )
    ]


def _inner_text(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    text = _as_str(data.get("delta") or data.get("content") or data.get("text") or "")
    return [AssistantTextDelta(type="assistant", text=text)] if text else []


def _inner_thinking(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    text = _as_str(data.get("delta") or data.get("content") or data.get("text") or "")
    return [ThinkingEvent(type="assistant", text=text)] if text else []


def _inner_toolcall_start(_event_type: str, _data: dict[str, Any]) -> list[StreamEvent]:
    return []


def _inner_toolcall_delta(_event_type: str, _data: dict[str, Any]) -> list[StreamEvent]:
    return []


def _inner_toolcall_end(_event_type: str, data: dict[str, Any]) -> list[StreamEvent]:
    tc = data.get("toolCall") if isinstance(data.get("toolCall"), dict) else data
    name = _as_str(tc.get("name") or "tool") if isinstance(tc, dict) else "tool"
    tool_id = _as_str(tc.get("id") or "") or None if isinstance(tc, dict) else None
    params = (
        tc.get("arguments")
        if isinstance(tc, dict) and isinstance(tc.get("arguments"), dict)
        else None
    )
    return [ToolUseEvent(type="assistant", tool_name=name, tool_id=tool_id, parameters=params)]


_TOP_LEVEL_HANDLERS: dict[str, Callable[[str, dict[str, Any]], list[StreamEvent]]] = {
    "session": _handle_session,
    "agent_start": _handle_agent_start,
    "agent_end": _handle_agent_end,
    "turn_start": _handle_turn_start,
    "turn_end": _handle_turn_end,
    "message_start": _handle_message_start,
    "message_end": _handle_message_end,
    "tool_execution_start": _handle_tool_execution_start,
    "tool_execution_update": _handle_tool_execution_update,
    "tool_execution_end": _handle_tool_execution_end,
    "error": _handle_error,
    "compact": _handle_compact,
    "compact_boundary": _handle_compact,
    "compaction": _handle_compact,
}

_INNER_HANDLERS: dict[str, Callable[[str, dict[str, Any]], list[StreamEvent]]] = {
    "text_start": lambda _t, _d: [],
    "text_delta": _inner_text,
    "text_end": lambda _t, _d: [],
    "thinking_start": lambda _t, _d: [],
    "thinking_delta": _inner_thinking,
    "thinking_end": lambda _t, _d: [],
    "reasoning_content": _inner_thinking,
    "reasoning_text": _inner_thinking,
    "toolcall_start": _inner_toolcall_start,
    "toolcall_delta": _inner_toolcall_delta,
    "toolcall_end": _inner_toolcall_end,
    "usage_update": lambda _t, _d: [],
    "citation_title": lambda _t, _d: [],
}


def _extract_spend(
    data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], float | None]:
    """Pull usage / modelUsage / total_cost_usd from an Omp payload."""
    usage: dict[str, Any] = {}
    model_usage: dict[str, Any] = {}
    total_cost: float | None = None

    raw_usage = data.get("usage")
    if isinstance(raw_usage, dict):
        usage = dict(raw_usage)
        if "input" in raw_usage and "input_tokens" not in usage:
            usage["input_tokens"] = raw_usage["input"]
        if "output" in raw_usage and "output_tokens" not in usage:
            usage["output_tokens"] = raw_usage["output"]
        if "totalTokens" in raw_usage and "total_tokens" not in usage:
            usage["total_tokens"] = raw_usage["totalTokens"]

    raw_model_usage = data.get("modelUsage") or data.get("model_usage")
    if isinstance(raw_model_usage, dict):
        model_usage = dict(raw_model_usage)

    for key in ("total_cost_usd", "totalCost", "cost", "total_cost"):
        val = data.get(key)
        if isinstance(val, (int, float)):
            total_cost = float(val)
            break
        if isinstance(val, dict) and isinstance(val.get("total"), (int, float)):
            total_cost = float(val["total"])
            break

    return usage, model_usage, total_cost


def _as_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
