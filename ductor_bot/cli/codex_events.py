"""JSONL output parser for the OpenAI Codex CLI."""

from __future__ import annotations

import json
import logging
from typing import Any

from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
    SystemInitEvent,
    ThinkingEvent,
    ToolUseEvent,
)

logger = logging.getLogger(__name__)


def _is_tagged_thinking(text: object) -> bool:
    """识别被 Codex 错标为普通助手消息的 thinking 内容."""
    return isinstance(text, str) and text.lstrip().lower().startswith("<thinking>")


def _tool_parameters(item: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("arguments", "parameters", "input"):
        value = item.get(key)
        if isinstance(value, dict):
            return value
    if isinstance(item.get("command"), str):
        return {"command": item["command"]}
    if isinstance(item.get("path"), str):
        return {"path": item["path"]}
    return None


def parse_codex_jsonl(raw: str) -> tuple[str, str | None, dict[str, Any] | None]:
    """Parse Codex JSONL output into (result_text, thread_id, usage)."""
    lines = raw.strip().splitlines()
    result_parts: list[str] = []
    thread_id: str | None = None
    usage: dict[str, Any] | None = None

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue

        data = _try_parse_json(stripped)
        if data is None:
            continue

        thread_id = _extract_thread_id(data, thread_id)
        usage = _extract_usage(data, usage)
        if _is_tool_boundary_event(data):
            result_parts.clear()
        _extract_text(data, result_parts)

    return "\n".join(result_parts).strip(), thread_id, usage


def _try_parse_json(line: str) -> dict[str, Any] | None:
    """Try to parse a line as JSON dict, return None on failure."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.debug("Codex: skipping unparseable JSONL line: %.200s", line)
        return None
    return data if isinstance(data, dict) else None


def _extract_thread_id(data: dict[str, Any], current: str | None) -> str | None:
    """Extract thread_id from thread.started event or top-level field."""
    if current is not None:
        return current
    if data.get("type") == "thread.started" and isinstance(data.get("thread_id"), str):
        tid: str = data["thread_id"]
        return tid.strip()
    if isinstance(data.get("thread_id"), str):
        fallback_tid: str = data["thread_id"]
        return fallback_tid.strip()
    return current


def _extract_usage(data: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract usage from turn.completed event; fall back to top-level only when unknown.

    ``turn.completed`` is the authoritative source.  Usage dicts on intermediate
    events are ignored so a later partial event cannot overwrite the final total.
    """
    if data.get("type") == "turn.completed":
        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
            return raw_usage
        # turn.completed without usage — keep whatever we have
        return current
    # Only use non-turn.completed usage when we have nothing yet
    if current is None:
        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
            return raw_usage
    return current


def _is_tool_item(data: dict[str, Any]) -> bool:
    """Return True if the event represents a tool invocation."""
    item = data.get("item")
    if not isinstance(item, dict):
        return False
    item_type = item.get("type", "")
    return item_type in _CODEX_ITEM_TOOL_MAP or item_type in {
        "mcp_tool_call",
        "collab_tool_call",
    }


def _is_tool_boundary_event(data: dict[str, Any]) -> bool:
    """识别 Codex 各类工具首次可见的事件边界."""
    if not _is_tool_item(data):
        return False
    item = data.get("item")
    if not isinstance(item, dict):
        return False
    if item.get("type") == "file_change":
        return data.get("type") in {"item.started", "item.completed"}
    return data.get("type") == "item.started"


def _extract_text(data: dict[str, Any], parts: list[str]) -> None:
    """Extract assistant text from Codex events.

    Only ``item.completed`` events are used for ``agent_message`` items to
    avoid triple-duplication across started/updated/completed.
    """
    event_type = data.get("type", "")

    if event_type in ("item.started", "item.updated", "item.completed"):
        _extract_item_text(data, parts, event_type)
        return

    if event_type == "message" and data.get("role") == "assistant":
        _extract_message_blocks(data, parts)
        return

    _extract_fallback_text(data, parts)


def _extract_item_text(data: dict[str, Any], parts: list[str], event_type: str) -> None:
    """Extract text from item events (only ``item.completed`` for agent messages)."""
    item = data.get("item")
    if (
        isinstance(item, dict)
        and item.get("type") == "agent_message"
        and event_type == "item.completed"
    ):
        text = item.get("text", "")
        if text and not _is_tagged_thinking(text):
            parts.append(text)


def _extract_message_blocks(data: dict[str, Any], parts: list[str]) -> None:
    """Extract text blocks from a ``message`` event."""
    for block in data.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text and not _is_tagged_thinking(text):
                parts.append(text)


def _extract_fallback_text(data: dict[str, Any], parts: list[str]) -> None:
    """Fallback: extract text from items with no explicit event type."""
    item = data.get("item")
    if isinstance(item, dict) and isinstance(item.get("text"), str):
        item_type = str(item.get("type", "")).lower()
        if item_type in ("", "agent_message") and not _is_tagged_thinking(item["text"]):
            parts.append(item["text"])


# -- Normalised single-line parser --

_CODEX_ITEM_TOOL_MAP: dict[str, str] = {
    "command_execution": "Bash",
    "file_change": "Edit",
    "web_search": "WebSearch",
    "todo_list": "TodoWrite",
}


def parse_codex_stream_event(line: str) -> list[StreamEvent]:
    """Parse a single Codex JSONL line into normalised stream events."""
    stripped = line.strip()
    if not stripped:
        return []

    data = _try_parse_json(stripped)
    if data is None:
        logger.warning("Codex line unparseable: %s", stripped[:100])
        return []

    return _dispatch_codex_event(data)


def _dispatch_codex_event(data: dict[str, Any]) -> list[StreamEvent]:
    """Route a parsed Codex event to the appropriate handler."""
    event_type = data.get("type", "")

    if event_type == "thread.started":
        logger.debug("Codex event parsed type=%s", event_type)
        tid = data.get("thread_id")
        return [
            SystemInitEvent(
                type="system",
                subtype="init",
                session_id=tid if isinstance(tid, str) else None,
            ),
        ]

    if event_type == "turn.completed":
        logger.debug("Codex event parsed type=%s", event_type)
        raw_usage = data.get("usage")
        return [
            ResultEvent(
                type="result",
                usage=raw_usage if isinstance(raw_usage, dict) else {},
            ),
        ]

    if event_type == "turn.failed":
        logger.debug("Codex event parsed type=%s", event_type)
        error = data.get("error", {})
        msg = error.get("message", "") if isinstance(error, dict) else ""
        return [ResultEvent(type="result", result=msg, is_error=True)]

    if event_type in ("item.started", "item.updated", "item.completed"):
        return _parse_codex_item(data)

    return []


def _parse_codex_item(data: dict[str, Any]) -> list[StreamEvent]:
    """Convert a Codex item event into normalised stream events.

    ``agent_message`` text is only emitted from ``item.completed`` to avoid
    triple-duplication.  Tool indicators are emitted from ``item.started``
    so they appear immediately.
    """
    item = data.get("item")
    if not isinstance(item, dict):
        return []

    event_type = data.get("type", "")
    item_type = item.get("type", "")

    if item_type == "agent_message":
        if event_type != "item.completed":
            return []
        text = item.get("text", "")
        if _is_tagged_thinking(text):
            return [ThinkingEvent(type="assistant", text=text)]
        return [AssistantTextDelta(type="assistant", text=text)] if text else []

    if item_type == "reasoning":
        return [ThinkingEvent(type="assistant", text=item.get("text", ""))]

    return _parse_tool_item(item, item_type, event_type)


def _parse_tool_item(item: dict[str, Any], item_type: str, event_type: str) -> list[StreamEvent]:
    """Extract a tool indicator from the first event Codex emits for the tool."""
    valid_events = (
        {"item.started", "item.completed"} if item_type == "file_change" else {"item.started"}
    )
    if event_type not in valid_events:
        return []
    if item_type in {"mcp_tool_call", "collab_tool_call"}:
        fallback_name = "MCP" if item_type == "mcp_tool_call" else "Agent"
        name = item.get("name") or item.get("tool_name") or item.get("tool") or fallback_name
        return [
            ToolUseEvent(
                type="assistant",
                tool_name=str(name),
                tool_id=item.get("id"),
                parameters=_tool_parameters(item),
            )
        ]
    tool_name = _CODEX_ITEM_TOOL_MAP.get(item_type)
    return (
        [
            ToolUseEvent(
                type="assistant",
                tool_name=tool_name,
                tool_id=item.get("id"),
                parameters=_tool_parameters(item),
            )
        ]
        if tool_name
        else []
    )


class CodexThinkingFilter:
    """Suppress intermediate agent text that precedes tool calls.

    Buffers ``AssistantTextDelta`` events.  When a ``ToolUseEvent`` arrives the
    buffered text is discarded (it was the model "thinking aloud" before a tool
    call).  When any other non-thinking event arrives the buffer is flushed
    first so final response text is preserved.
    """

    def __init__(self) -> None:
        self._buffered: list[StreamEvent] = []

    def process(self, event: StreamEvent) -> list[StreamEvent]:
        """Process one event, returning zero or more events to emit."""
        if isinstance(event, AssistantTextDelta):
            self._buffered.append(event)
            return []

        if isinstance(event, ToolUseEvent):
            self._buffered.clear()
            return [event]

        if isinstance(event, ThinkingEvent):
            return [event]

        result = list(self._buffered)
        self._buffered.clear()
        result.append(event)
        return result

    def flush(self) -> list[StreamEvent]:
        """Flush remaining buffered events (call at stream end)."""
        events = list(self._buffered)
        self._buffered.clear()
        return events
