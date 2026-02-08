"""Tests for stream event models and NDJSON parser."""

from __future__ import annotations

import json

from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
    SystemInitEvent,
    ThinkingEvent,
    ToolUseEvent,
    parse_stream_line,
)

# -- parse_stream_line --


def test_empty_line_returns_empty() -> None:
    assert parse_stream_line("") == []
    assert parse_stream_line("   ") == []


def test_invalid_json_returns_empty() -> None:
    assert parse_stream_line("not json") == []


def test_parse_result_event() -> None:
    data = {
        "type": "result",
        "session_id": "abc-123",
        "result": "Done.",
        "is_error": False,
        "duration_ms": 1500.0,
        "total_cost_usd": 0.05,
        "usage": {"input_tokens": 500, "output_tokens": 200},
        "num_turns": 3,
    }
    events = parse_stream_line(json.dumps(data))
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ResultEvent)
    assert event.session_id == "abc-123"
    assert event.result == "Done."
    assert event.is_error is False
    assert event.total_cost_usd == 0.05
    assert event.num_turns == 3


def test_parse_system_init_event() -> None:
    data = {"type": "system", "subtype": "init", "session_id": "sess-1"}
    events = parse_stream_line(json.dumps(data))
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, SystemInitEvent)
    assert event.session_id == "sess-1"


def test_parse_assistant_text() -> None:
    data = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Hello world"}]},
    }
    events = parse_stream_line(json.dumps(data))
    assert len(events) == 1
    assert isinstance(events[0], AssistantTextDelta)
    assert events[0].text == "Hello world"


def test_parse_assistant_tool_use() -> None:
    data = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Read"}]},
    }
    events = parse_stream_line(json.dumps(data))
    assert len(events) == 1
    assert isinstance(events[0], ToolUseEvent)
    assert events[0].tool_name == "Read"


def test_parse_assistant_thinking() -> None:
    data = {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "text": "Let me think..."}]},
    }
    events = parse_stream_line(json.dumps(data))
    assert len(events) == 1
    assert isinstance(events[0], ThinkingEvent)
    assert events[0].text == "Let me think..."


def test_parse_multiple_content_blocks() -> None:
    data = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Part 1"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "text", "text": "Part 2"},
            ]
        },
    }
    events = parse_stream_line(json.dumps(data))
    assert len(events) == 3
    assert isinstance(events[0], AssistantTextDelta)
    assert isinstance(events[1], ToolUseEvent)
    assert isinstance(events[2], AssistantTextDelta)


def test_parse_empty_text_skipped() -> None:
    data = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": ""}]},
    }
    events = parse_stream_line(json.dumps(data))
    assert events == []


def test_parse_unknown_type_returns_empty() -> None:
    data = {"type": "unknown_type"}
    assert parse_stream_line(json.dumps(data)) == []


def test_non_init_system_event_ignored() -> None:
    data = {"type": "system", "subtype": "other"}
    assert parse_stream_line(json.dumps(data)) == []


def test_result_event_defaults() -> None:
    data = {"type": "result"}
    events = parse_stream_line(json.dumps(data))
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ResultEvent)
    assert event.result == ""
    assert event.is_error is False
    assert event.session_id is None


# -- StreamEvent base --


def test_stream_event_type() -> None:
    e = StreamEvent(type="test")
    assert e.type == "test"
    assert e.subtype is None
