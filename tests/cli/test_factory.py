"""Tests for cli/factory.py: create_cli backend selection."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.claude_provider import ClaudeCodeCLI
from ductor_bot.cli.codex_provider import CodexCLI
from ductor_bot.cli.factory import create_cli
from ductor_bot.cli.gemini_provider import GeminiCLI


def test_create_cli_returns_claude_by_default() -> None:
    with patch("ductor_bot.cli.claude_provider.which", return_value="/usr/bin/claude"):
        cli = create_cli(CLIConfig(provider="claude"))
    assert isinstance(cli, ClaudeCodeCLI)


def test_create_cli_returns_codex() -> None:
    cli = create_cli(CLIConfig(provider="codex"))
    assert isinstance(cli, CodexCLI)


def test_create_cli_returns_gemini() -> None:
    with (
        patch("ductor_bot.cli.gemini_provider.find_gemini_cli", return_value="/usr/bin/gemini"),
        patch("ductor_bot.cli.gemini_provider.find_gemini_cli_js", return_value=None),
    ):
        cli = create_cli(CLIConfig(provider="gemini"))
    assert isinstance(cli, GeminiCLI)


def test_create_cli_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported provider"):
        create_cli(CLIConfig(provider="unknown"))


def test_create_cli_rejects_provider_model_combined_value() -> None:
    with pytest.raises(ValueError, match="separate fields"):
        create_cli(CLIConfig(provider="codex/gpt-5.6-luna"))
