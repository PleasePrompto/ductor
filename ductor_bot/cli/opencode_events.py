"""NDJSON parser for the OpenCode CLI.

Translates OpenCode-specific events into normalized StreamEvents.
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
    SystemInitEvent,
    ToolResultEvent,
    ToolUseEvent,
)

logger = logging.getLogger(__name__)


def parse_opencode_stream_line(line: str) -> list[StreamEvent]:
    """Parse a single NDJSON line from OpenCode CLI into normalized stream events."""
    stripped = line.strip()
    if not stripped:
        return []

    try:
        data: dict[str, Any] = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("OpenCode: unparseable stream line: %.200s", stripped)
        return []

    parser = _STREAM_PARSERS.get(data.get("type", ""))
    return parser(data) if parser else []


def parse_opencode_json(raw: str) -> str:
    """Extract result text from OpenCode CLI JSON batch output (non-streaming)."""
    if not raw:
        return ""
    raw = raw.strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:2000]

    if isinstance(parsed, dict):
        return _extract_result_text(parsed)

    if isinstance(parsed, list):
        texts = [_extract_result_text(item) for item in parsed if isinstance(item, dict)]
        return "\n\n".join(text for text in texts if text)

    return ""


def _extract_result_text(data: dict[str, Any]) -> str:
    """Extract result text from an OpenCode response dict."""
    for key in ("result", "response", "content", "output", "text"):
        value = data.get(key)
        if value is not None:
            return value if isinstance(value, str) else str(value)
    return ""


def _parse_opencode_message(data: dict[str, Any]) -> list[StreamEvent]:
    """Parse OpenCode message event."""
    role = data.get("role")
    content = data.get("content")
    if role not in ("assistant", "model") or not content:
        return []

    if isinstance(content, str):
        return [AssistantTextDelta(type="assistant", text=content)]

    if isinstance(content, list):
        events: list[StreamEvent] = []
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type")
                if block_type == "text":
                    text = block.get("text", "")
                    if text:
                        events.append(AssistantTextDelta(type="assistant", text=str(text)))
                elif block_type == "tool_use":
                    events.append(
                        ToolUseEvent(
                            type="assistant",
                            tool_name=str(block.get("name", "") or block.get("tool_name", "")),
                            tool_id=_str_or_none(block.get("id") or block.get("tool_id")),
                            parameters=block.get("input") or block.get("parameters"),
                        )
                    )
        return events

    return []


def _parse_opencode_init(data: dict[str, Any]) -> list[StreamEvent]:
    return [
        SystemInitEvent(
            type="system",
            subtype="init",
            session_id=data.get("session_id") or data.get("id"),
        ),
    ]


def _parse_opencode_tool_use(data: dict[str, Any]) -> list[StreamEvent]:
    return [
        ToolUseEvent(
            type="assistant",
            tool_name=str(data.get("tool_name") or data.get("name") or ""),
            tool_id=_str_or_none(data.get("tool_id") or data.get("id")),
            parameters=data.get("parameters") or data.get("input"),
        ),
    ]


def _parse_opencode_tool_result(data: dict[str, Any]) -> list[StreamEvent]:
    return [
        ToolResultEvent(
            type="tool_result",
            tool_id=str(data.get("tool_id", "")),
            status=str(data.get("status", "")),
            output=str(data.get("output", "")),
        ),
    ]


def _parse_opencode_result(data: dict[str, Any]) -> list[StreamEvent]:
    """Extract metrics and final output from OpenCode's result event."""
    stats = data.get("stats", {})
    if not isinstance(stats, dict):
        stats = {}

    usage = {
        "input_tokens": stats.get("input_tokens", 0),
        "output_tokens": stats.get("output_tokens", 0),
        "cached_tokens": stats.get("cached_tokens", 0),
    }

    is_error = bool(data.get("is_error")) or data.get("status") == "error"
    res = _extract_result_text(data)

    if not res and is_error:
        err = data.get("error")
        if isinstance(err, dict):
            res = _extract_error_text(err)
        elif err is not None:
            res = str(err)

    return [
        ResultEvent(
            type="result",
            session_id=data.get("session_id") or data.get("id"),
            result=res or "",
            is_error=is_error,
            duration_ms=stats.get("duration_ms"),
            usage=usage,
        ),
    ]


def _parse_opencode_error(data: dict[str, Any]) -> list[StreamEvent]:
    return [
        ResultEvent(
            type="result",
            result=_extract_error_text(data) or "Unknown OpenCode error",
            is_error=True,
        ),
    ]


def _extract_error_text(data: dict[str, Any]) -> str:
    for key in ("message", "error", "detail"):
        value = data.get(key)
        if value is not None:
            return value if isinstance(value, str) else str(value)
    return ""


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _stream_event_from_dict(data: dict[str, Any]) -> list[StreamEvent]:
    """Parse a generic OpenCode stream event dict."""
    return _parse_opencode_result(data)


_StreamParser = Callable[[dict[str, Any]], list[StreamEvent]]

_STREAM_PARSERS: dict[str, _StreamParser] = {
    "init": _parse_opencode_init,
    "message": _parse_opencode_message,
    "tool_use": _parse_opencode_tool_use,
    "tool_result": _parse_opencode_tool_result,
    "result": _parse_opencode_result,
    "error": _parse_opencode_error,
    "assistant": _parse_opencode_message,
    "stream": _stream_event_from_dict,
}
