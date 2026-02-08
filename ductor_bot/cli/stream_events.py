"""Stream event models and NDJSON parser for --output-format stream-json."""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class StreamEvent(BaseModel):
    """Base event from the Claude CLI stream-json output."""

    type: str
    subtype: str | None = None


class AssistantTextDelta(StreamEvent):
    """Text from an assistant turn."""

    text: str = ""


class SystemInitEvent(StreamEvent):
    """First event of a stream -- contains session_id and tool list."""

    session_id: str | None = None


class ResultEvent(StreamEvent):
    """Final event with usage, cost, and session_id."""

    session_id: str | None = None
    result: str = ""
    is_error: bool = False
    duration_ms: float | None = None
    duration_api_ms: float | None = None
    total_cost_usd: float | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    model_usage: dict[str, Any] = Field(default_factory=dict)
    num_turns: int | None = None


class ToolUseEvent(StreamEvent):
    """Tool invocation detected during streaming."""

    tool_name: str = ""


class ThinkingEvent(StreamEvent):
    """Extended thinking/reasoning block."""

    text: str = ""


class SystemStatusEvent(StreamEvent):
    """System status update (e.g. ``compacting``)."""

    status: str | None = None


class CompactBoundaryEvent(StreamEvent):
    """Marks a context compaction boundary."""

    trigger: str = ""
    pre_tokens: int = 0


def parse_stream_line(line: str) -> list[StreamEvent]:
    """Parse a single NDJSON line into normalized stream events."""
    stripped = line.strip()
    if not stripped:
        return []

    try:
        data: dict[str, Any] = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("Unparseable stream line: %.200s", stripped)
        return []

    event_type = data.get("type", "")

    if event_type == "result":
        logger.debug("Stream event parsed type=%s", event_type)
        return [
            ResultEvent(
                type=event_type,
                subtype=data.get("subtype"),
                session_id=data.get("session_id"),
                result=data.get("result", ""),
                is_error=data.get("is_error", False),
                duration_ms=data.get("duration_ms"),
                duration_api_ms=data.get("duration_api_ms"),
                total_cost_usd=data.get("total_cost_usd"),
                usage=data.get("usage", {}),
                model_usage=data.get("modelUsage", {}),
                num_turns=data.get("num_turns"),
            ),
        ]

    if event_type == "assistant":
        return _parse_assistant_content(data)

    if event_type == "system":
        logger.debug("Stream event parsed type=%s subtype=%s", event_type, data.get("subtype"))
        return _parse_system_event(data)

    return []


def _parse_system_event(data: dict[str, Any]) -> list[StreamEvent]:
    """Route system events by subtype."""
    subtype = data.get("subtype", "")

    if subtype == "init":
        return [
            SystemInitEvent(
                type="system",
                subtype="init",
                session_id=data.get("session_id"),
            ),
        ]

    if subtype == "status":
        return [
            SystemStatusEvent(
                type="system",
                subtype="status",
                status=data.get("status"),
            ),
        ]

    if subtype == "compact_boundary":
        meta = data.get("compact_metadata", {})
        return [
            CompactBoundaryEvent(
                type="system",
                subtype="compact_boundary",
                trigger=meta.get("trigger", ""),
                pre_tokens=meta.get("pre_tokens", 0),
            ),
        ]

    return []


def _parse_assistant_content(data: dict[str, Any]) -> list[StreamEvent]:
    """Extract all content blocks from an assistant message."""
    message = data.get("message", {})
    content = message.get("content", [])
    events: list[StreamEvent] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")

        if block_type == "text":
            text = block.get("text", "")
            if text:
                events.append(AssistantTextDelta(type="assistant", text=text))

        elif block_type == "tool_use":
            name = block.get("name", "")
            if name:
                events.append(ToolUseEvent(type="assistant", tool_name=name))

        elif block_type == "thinking":
            events.append(
                ThinkingEvent(type="assistant", text=block.get("text", "")),
            )

    return events
