"""Tests for cron/execution.py: CLI command building and output parsing."""

from __future__ import annotations

from unittest.mock import patch

from ductor_bot.cron.execution import (
    build_cmd,
    enrich_instruction,
    indent,
    parse_claude_result,
    parse_codex_result,
)


class TestBuildCmd:
    def test_claude_provider(self) -> None:
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            cmd = build_cmd("claude", "opus", "hello", "bypassPermissions")
        assert cmd is not None
        assert cmd[0] == "/usr/bin/claude"
        assert "--no-session-persistence" in cmd

    def test_codex_provider(self) -> None:
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
            cmd = build_cmd("codex", "gpt-4", "hello", "bypassPermissions")
        assert cmd is not None
        assert cmd[0] == "/usr/bin/codex"
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd

    def test_codex_full_auto(self) -> None:
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
            cmd = build_cmd("codex", "gpt-4", "hello", "plan")
        assert cmd is not None
        assert "--full-auto" in cmd

    def test_returns_none_when_cli_missing(self) -> None:
        with patch("ductor_bot.cron.execution.which", return_value=None):
            assert build_cmd("claude", "opus", "hello", "plan") is None

    def test_unknown_provider_falls_back_to_claude(self) -> None:
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            cmd = build_cmd("unknown", "model", "hello", "plan")
        assert cmd is not None
        assert cmd[0] == "/usr/bin/claude"


class TestEnrichInstruction:
    def test_appends_memory_instructions(self) -> None:
        result = enrich_instruction("Do the work", "daily-report")
        assert "daily-report_MEMORY.md" in result
        assert "Do the work" in result

    def test_preserves_original(self) -> None:
        original = "Original instruction"
        result = enrich_instruction(original, "weekly")
        assert result.startswith(original)


class TestParseClaude:
    def test_parses_json(self) -> None:
        import json

        stdout = json.dumps({"result": "Hello world"}).encode()
        assert parse_claude_result(stdout) == "Hello world"

    def test_empty_bytes(self) -> None:
        assert parse_claude_result(b"") == ""

    def test_non_json_returns_raw(self) -> None:
        raw = b"Some raw text output"
        assert parse_claude_result(raw) == "Some raw text output"


class TestParseCodex:
    def test_empty_bytes(self) -> None:
        assert parse_codex_result(b"") == ""


class TestIndent:
    def test_indents_lines(self) -> None:
        result = indent("a\nb\nc", "  ")
        assert result == "  a\n  b\n  c"

    def test_single_line(self) -> None:
        assert indent("hello", ">> ") == ">> hello"
