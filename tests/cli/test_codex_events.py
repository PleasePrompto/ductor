"""Tests for Codex JSONL event parsing."""

from __future__ import annotations

import json

from ductor_bot.cli.codex_events import parse_codex_jsonl, parse_codex_stream_event
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    SystemInitEvent,
    ThinkingEvent,
    ToolUseEvent,
)

# -- parse_codex_jsonl (batch) --


def test_parse_empty_returns_empty() -> None:
    text, tid, usage = parse_codex_jsonl("")
    assert text == ""
    assert tid is None
    assert usage is None


def test_parse_thread_started() -> None:
    line = json.dumps({"type": "thread.started", "thread_id": "th-123"})
    _, tid, _ = parse_codex_jsonl(line)
    assert tid == "th-123"


def test_parse_agent_message_text() -> None:
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Hello!"},
        }
    )
    text, _, _ = parse_codex_jsonl(line)
    assert text == "Hello!"


def test_parse_usage_from_turn_completed() -> None:
    line = json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    )
    _, _, usage = parse_codex_jsonl(line)
    assert usage is not None
    assert usage["input_tokens"] == 100


def test_parse_multiple_lines() -> None:
    lines = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "th-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Part 1"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Part 2"},
                }
            ),
        ]
    )
    text, tid, _ = parse_codex_jsonl(lines)
    assert tid == "th-1"
    assert "Part 1" in text
    assert "Part 2" in text


def test_unparseable_lines_skipped() -> None:
    raw = "not json\n" + json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "OK"},
        }
    )
    text, _, _ = parse_codex_jsonl(raw)
    assert text == "OK"


# -- parse_codex_stream_event (single line) --


def test_stream_empty_line() -> None:
    assert parse_codex_stream_event("") == []
    assert parse_codex_stream_event("   ") == []


def test_stream_invalid_json() -> None:
    assert parse_codex_stream_event("not json") == []


def test_stream_thread_started() -> None:
    line = json.dumps({"type": "thread.started", "thread_id": "th-abc"})
    events = parse_codex_stream_event(line)
    assert len(events) == 1
    assert isinstance(events[0], SystemInitEvent)
    assert events[0].session_id == "th-abc"


def test_stream_turn_completed() -> None:
    line = json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 200},
        }
    )
    events = parse_codex_stream_event(line)
    assert len(events) == 1
    assert isinstance(events[0], ResultEvent)
    assert events[0].usage["input_tokens"] == 200


def test_stream_turn_failed() -> None:
    line = json.dumps(
        {
            "type": "turn.failed",
            "error": {"message": "Rate limited"},
        }
    )
    events = parse_codex_stream_event(line)
    assert len(events) == 1
    assert isinstance(events[0], ResultEvent)
    assert events[0].is_error is True
    assert events[0].result == "Rate limited"


def test_stream_agent_message() -> None:
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Hello"},
        }
    )
    events = parse_codex_stream_event(line)
    assert len(events) == 1
    assert isinstance(events[0], AssistantTextDelta)
    assert events[0].text == "Hello"


def test_stream_reasoning() -> None:
    line = json.dumps(
        {
            "type": "item.started",
            "item": {"type": "reasoning", "text": "Thinking..."},
        }
    )
    events = parse_codex_stream_event(line)
    assert len(events) == 1
    assert isinstance(events[0], ThinkingEvent)


def test_stream_command_execution() -> None:
    line = json.dumps(
        {
            "type": "item.started",
            "item": {"type": "command_execution"},
        }
    )
    events = parse_codex_stream_event(line)
    assert len(events) == 1
    assert isinstance(events[0], ToolUseEvent)
    assert events[0].tool_name == "Bash"


def test_stream_file_change() -> None:
    line = json.dumps(
        {
            "type": "item.started",
            "item": {"type": "file_change"},
        }
    )
    events = parse_codex_stream_event(line)
    assert len(events) == 1
    assert isinstance(events[0], ToolUseEvent)
    assert events[0].tool_name == "Edit"


def test_stream_mcp_tool_call() -> None:
    line = json.dumps(
        {
            "type": "item.started",
            "item": {"type": "mcp_tool_call", "name": "search_docs"},
        }
    )
    events = parse_codex_stream_event(line)
    assert len(events) == 1
    assert isinstance(events[0], ToolUseEvent)
    assert events[0].tool_name == "search_docs"


def test_stream_unknown_type_returns_empty() -> None:
    line = json.dumps({"type": "something.else"})
    assert parse_codex_stream_event(line) == []
