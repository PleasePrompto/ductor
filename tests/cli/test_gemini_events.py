"""Tests for Gemini-specific stream event parsing."""

from __future__ import annotations

import json

from ductor_bot.cli.gemini_events import parse_gemini_json, parse_gemini_stream_line
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    SystemInitEvent,
    ThinkingEvent,
    ToolUseEvent,
)


def test_parse_gemini_init() -> None:
    data = {"type": "init", "session_id": "gem-123"}
    events = parse_gemini_stream_line(json.dumps(data))
    assert len(events) == 1
    assert isinstance(events[0], SystemInitEvent)
    assert events[0].session_id == "gem-123"


def test_parse_flat_gemini_message() -> None:
    data = {"type": "message", "role": "assistant", "content": "Hello Gemini"}
    events = parse_gemini_stream_line(json.dumps(data))
    assert len(events) == 1
    assert isinstance(events[0], AssistantTextDelta)
    assert events[0].text == "Hello Gemini"


def test_parse_flat_gemini_tool_use() -> None:
    data = {
        "type": "tool_use",
        "tool_name": "bash",
        "tool_id": "bash_1",
        "parameters": {"cmd": "ls"},
    }
    events = parse_gemini_stream_line(json.dumps(data))
    assert len(events) == 1
    assert isinstance(events[0], ToolUseEvent)
    assert events[0].tool_name == "bash"
    assert events[0].tool_id == "bash_1"
    assert events[0].parameters == {"cmd": "ls"}


def test_parse_gemini_result_with_stats() -> None:
    data = {
        "type": "result",
        "status": "success",
        "response": "Done!",
        "session_id": "s-1",
        "stats": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cached": 20,
            "duration_ms": 1234,
        },
    }
    events = parse_gemini_stream_line(json.dumps(data))
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ResultEvent)
    assert event.result == "Done!"
    assert event.session_id == "s-1"
    assert event.usage["cached_tokens"] == 20
    assert event.duration_ms == 1234
    assert event.is_error is False


def test_parse_gemini_result_uses_result_field() -> None:
    data = {"type": "result", "status": "success", "result": "from-result-field"}
    events = parse_gemini_stream_line(json.dumps(data))
    assert len(events) == 1
    assert isinstance(events[0], ResultEvent)
    assert events[0].result == "from-result-field"


def test_parse_gemini_error() -> None:
    data = {"type": "error", "message": "API Key Invalid"}
    events = parse_gemini_stream_line(json.dumps(data))
    assert len(events) == 1
    assert isinstance(events[0], ResultEvent)
    assert events[0].is_error is True
    assert events[0].result == "API Key Invalid"


def test_parse_gemini_nested_message_list() -> None:
    data = {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Here is some code:"},
            {"type": "tool_use", "name": "bash", "id": "b1", "input": {"cmd": "ls"}},
        ],
    }
    events = parse_gemini_stream_line(json.dumps(data))
    assert len(events) == 2
    assert isinstance(events[0], AssistantTextDelta)
    assert events[0].text == "Here is some code:"
    assert isinstance(events[1], ToolUseEvent)
    assert events[1].tool_name == "bash"
    assert events[1].tool_id == "b1"
    assert events[1].parameters == {"cmd": "ls"}


def test_parse_gemini_message_strips_thought_marker() -> None:
    """Marker-only text block should become a single ThinkingEvent (#110)."""
    data = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "[Thought: true]"}],
    }
    events = parse_gemini_stream_line(json.dumps(data))
    assert len(events) == 1
    assert isinstance(events[0], ThinkingEvent)
    assert events[0].text == "[Thought: true]"


def test_parse_gemini_message_thought_marker_with_content_splits_events() -> None:
    """Marker+content text block should split into ThinkingEvent + AssistantTextDelta (#110)."""
    data = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "[Thought: true]\nHello user"}],
    }
    events = parse_gemini_stream_line(json.dumps(data))
    assert len(events) == 2
    assert isinstance(events[0], ThinkingEvent)
    assert events[0].text == "[Thought: true]"
    assert isinstance(events[1], AssistantTextDelta)
    assert events[1].text == "Hello user"


def test_parse_gemini_message_plain_text_unchanged() -> None:
    """Non-marker text must not be rerouted through ThinkingEvent (regression guard)."""
    data = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "plain content"}],
    }
    events = parse_gemini_stream_line(json.dumps(data))
    assert len(events) == 1
    assert isinstance(events[0], AssistantTextDelta)
    assert events[0].text == "plain content"


def test_parse_gemini_result_error_with_details() -> None:
    data = {
        "type": "result",
        "status": "error",
        "error": {"message": "Quota exceeded", "code": 429},
    }
    events = parse_gemini_stream_line(json.dumps(data))
    assert len(events) == 1
    assert isinstance(events[0], ResultEvent)
    assert events[0].is_error is True
    assert events[0].result == "Quota exceeded"


def test_parse_gemini_empty_or_invalid() -> None:
    assert parse_gemini_stream_line("") == []
    assert parse_gemini_stream_line("   ") == []
    assert parse_gemini_stream_line("not json") == []
    assert parse_gemini_stream_line('{"type": "unknown"}') == []


def test_parse_gemini_message_invalid_role() -> None:
    data = {"type": "message", "role": "user", "content": "Hello"}
    assert parse_gemini_stream_line(json.dumps(data)) == []


def test_parse_gemini_message_invalid_content_type() -> None:
    data = {"type": "message", "role": "assistant", "content": 123}
    assert parse_gemini_stream_line(json.dumps(data)) == []


# -- parse_gemini_json (batch mode) --


def test_parse_gemini_json_response_field() -> None:
    data = {"response": "Hello world", "status": "success"}
    assert parse_gemini_json(json.dumps(data)) == "Hello world"


def test_parse_gemini_json_content_field() -> None:
    data = {"content": "Fallback content"}
    assert parse_gemini_json(json.dumps(data)) == "Fallback content"


def test_parse_gemini_json_output_field() -> None:
    data = {"output": "Output text"}
    assert parse_gemini_json(json.dumps(data)) == "Output text"


def test_parse_gemini_json_result_field() -> None:
    data = {"result": "Result text"}
    assert parse_gemini_json(json.dumps(data)) == "Result text"


def test_parse_gemini_json_list_prefers_result_text() -> None:
    data = [{"result": "A"}, {"response": "B"}, {"content": "C"}, {"output": "D"}]
    assert parse_gemini_json(json.dumps(data)) == "A\n\nB\n\nC\n\nD"


def test_parse_gemini_json_empty() -> None:
    assert parse_gemini_json("") == ""
    assert parse_gemini_json("   ") == ""


def test_parse_gemini_json_invalid() -> None:
    result = parse_gemini_json("not json at all")
    assert result == "not json at all"


def test_parse_gemini_json_no_result_fields() -> None:
    data = {"status": "success"}
    assert parse_gemini_json(json.dumps(data)) == ""
