"""NDJSON parser for the Google Gemini CLI.
Translates Gemini-specific events into normalized StreamEvents.
"""

from __future__ import annotations

import json
import logging
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


def parse_gemini_stream_line(line: str) -> list[StreamEvent]:
    """Parse a single NDJSON line from Gemini CLI into normalized stream events."""
    stripped = line.strip()
    if not stripped:
        return []

    try:
        data: dict[str, Any] = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("Gemini: unparseable stream line: %.200s", stripped)
        return []

    etype = data.get("type", "")

    if etype == "init":
        return [
            SystemInitEvent(
                type="system",
                subtype="init",
                session_id=data.get("session_id"),
            )
        ]

    if etype == "message":
        return _parse_gemini_message(data)

    if etype == "tool_use":
        return [
            ToolUseEvent(
                type="assistant",
                tool_name=data.get("tool_name", ""),
                tool_id=data.get("tool_id"),
                parameters=data.get("parameters", {}),
            )
        ]

    if etype == "tool_result":
        return [
            ToolResultEvent(
                type="tool_result",
                tool_id=data.get("tool_id"),
                status=data.get("status", ""),
                output=data.get("output", ""),
            )
        ]

    if etype == "result":
        return [_parse_gemini_result(data)]

    if etype == "error":
        return [
            ResultEvent(
                type="result",
                result=data.get("message", "Unknown Gemini error"),
                is_error=True,
            )
        ]

    return []


def _parse_gemini_message(data: dict[str, Any]) -> list[StreamEvent]:
    """Parse Gemini's flat message structure."""
    role = data.get("role")
    content = data.get("content")
    if role != "assistant" or not content:
        return []

    if isinstance(content, str):
        return [AssistantTextDelta(type="assistant", text=content)]

    if isinstance(content, list):
        events: list[StreamEvent] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            b_type = block.get("type")
            if b_type == "text":
                events.append(AssistantTextDelta(type="assistant", text=block.get("text", "")))
            elif b_type == "tool_use":
                events.append(
                    ToolUseEvent(
                        type="assistant",
                        tool_name=block.get("name", ""),
                        tool_id=block.get("id"),
                        parameters=block.get("input", {}),
                    )
                )
        return events

    return []


def _parse_gemini_result(data: dict[str, Any]) -> ResultEvent:
    """Extract metrics and final output from Gemini's result event."""
    stats = data.get("stats", {})
    usage = {
        "input_tokens": stats.get("input_tokens", 0),
        "output_tokens": stats.get("output_tokens", 0),
        "cached_tokens": stats.get("cached", 0),
    }

    # Gemini result can have status: error
    is_error = data.get("status") == "error"
    res = data.get("response") or data.get("content") or data.get("output")

    if not res and is_error:
        err = data.get("error")
        res = err.get("message") if isinstance(err, dict) else str(err)

    return ResultEvent(
        type="result",
        session_id=data.get("session_id"),
        result=res or "",
        is_error=is_error,
        duration_ms=stats.get("duration_ms"),
        usage=usage,
    )
