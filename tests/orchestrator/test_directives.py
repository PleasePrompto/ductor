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


def test_opencode_provider_alias_model_directive() -> None:
    result = parse_directives("@go/model-alpha hello", KNOWN)
    assert result.model == "opencode-go/model-alpha"
    assert result.cleaned == "hello"

    result = parse_directives("@zen/model-beta hello", KNOWN)
    assert result.model == "opencode/model-beta"
    assert result.cleaned == "hello"


def test_opencode_alias_preserves_nested_model_slashes() -> None:
    result = parse_directives("@or/provider/model-gamma hello", KNOWN)
    assert result.model == "openrouter/provider/model-gamma"
    assert result.cleaned == "hello"


def test_opencode_directive_preserves_colon_suffix() -> None:
    model = "openrouter/qwen/qwen-plus:free"
    result = parse_directives(f"@{model} hello", frozenset({model}))
    assert result.model == model
    assert result.cleaned == "hello"


def test_parsed_directives_defaults() -> None:
    pd = ParsedDirectives(cleaned="test")
    assert not pd.has_model
    assert not pd.is_directive_only
    assert pd.raw_directives == {}
