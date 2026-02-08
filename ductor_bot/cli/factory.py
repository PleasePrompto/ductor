"""CLI backend factory -- returns the right provider based on config."""

from __future__ import annotations

import logging

from ductor_bot.cli.base import BaseCLI, CLIConfig

logger = logging.getLogger(__name__)


def create_cli(config: CLIConfig) -> BaseCLI:
    """Create a CLI backend instance based on ``config.provider``."""
    logger.debug("CLI factory creating provider=%s", config.provider)
    if config.provider == "codex":
        from ductor_bot.cli.codex_provider import CodexCLI

        return CodexCLI(config)

    from ductor_bot.cli.claude_provider import ClaudeCodeCLI

    return ClaudeCodeCLI(config)
