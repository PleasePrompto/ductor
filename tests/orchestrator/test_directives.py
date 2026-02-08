"""Tests for directive parsing."""

from __future__ import annotations

from ductor_bot.orchestrator.directives import ParsedDirectives, parse_directives

KNOWN = frozenset({"opus", "sonnet", "haiku"})


def test_no_directives() -> None:
    result = parse_directives("hello world", KNOWN)
    assert result.cleaned == "hello world"
    assert result.model is None
    assert not result.has_model


def test_model_directive() -> None:
    result = parse_directives("@opus hello world", KNOWN)
    assert result.model == "opus"
    assert result.cleaned == "hello world"


def test_directive_only() -> None:
    result = parse_directives("@opus", KNOWN)
    assert result.model == "opus"
    assert result.is_directive_only


def test_directive_not_at_start() -> None:
    result = parse_directives("hello @opus", KNOWN)
    assert result.model is None
    assert result.cleaned == "hello @opus"


def test_unknown_model_becomes_raw() -> None:
    result = parse_directives("@gpt4 hello", KNOWN)
    assert result.model is None
    assert "gpt4" in result.raw_directives


def test_key_value_directive() -> None:
    result = parse_directives("@opus @mode=fast hello", KNOWN)
    assert result.model == "opus"
    assert result.raw_directives.get("mode") == "fast"
    assert result.cleaned == "hello"


def test_empty_text() -> None:
    result = parse_directives("", KNOWN)
    assert result.cleaned == ""
    assert result.model is None


def test_whitespace_only() -> None:
    result = parse_directives("   ", KNOWN)
    assert result.cleaned == ""


def test_first_model_wins() -> None:
    result = parse_directives("@opus @sonnet hello", KNOWN)
    assert result.model == "opus"
    assert "sonnet" in result.raw_directives


def test_case_insensitive() -> None:
    result = parse_directives("@OPUS hello", KNOWN)
    assert result.model == "opus"


def test_parsed_directives_defaults() -> None:
    pd = ParsedDirectives(cleaned="test")
    assert not pd.has_model
    assert not pd.is_directive_only
    assert pd.raw_directives == {}
