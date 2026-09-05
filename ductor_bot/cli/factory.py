"""CLI backend factory -- returns the right provider based on config."""

from __future__ import annotations

import logging

from ductor_bot.cli.base import BaseCLI, CLIConfig

logger = logging.getLogger(__name__)

# Keep this list in one place so a malformed value such as
# ``codex/gpt-5.6-luna`` can never fall through to the Claude backend.
SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {"claude", "codex", "gemini", "antigravity", "grok"}
)


def validate_provider(provider: str) -> str:
    """Validate and return an exact provider name.

    Provider and model are deliberately separate fields.  In particular, do
    not accept the old ``provider/model`` spelling here: accepting it would
    make the factory's fallback behavior select an unrelated CLI.
    """
    if not isinstance(provider, str) or not provider:
        msg = "Provider is required and must be one of: " + ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ValueError(msg)
    if "/" in provider or "\\" in provider:
        raise ValueError(
            f"Invalid provider '{provider}': provider and model must be separate fields "
            "(for example provider='codex', model='gpt-5.6-luna')"
        )
    if provider not in SUPPORTED_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ValueError(f"Unsupported provider '{provider}'. Supported providers: {supported}")
    return provider


def create_cli(config: CLIConfig) -> BaseCLI:
    """Create a CLI backend instance based on ``config.provider``."""
    provider = validate_provider(config.provider)
    logger.debug("CLI factory creating provider=%s", provider)
    if provider == "gemini":
        from ductor_bot.cli.gemini_provider import GeminiCLI

        return GeminiCLI(config)

    if provider == "codex":
        from ductor_bot.cli.codex_provider import CodexCLI

        return CodexCLI(config)

    if provider == "antigravity":
        from ductor_bot.cli.antigravity_provider import AntigravityCLI

        return AntigravityCLI(config)

    if provider == "grok":
        from ductor_bot.cli.grok_provider import GrokCLI

        return GrokCLI(config)

    from ductor_bot.cli.claude_provider import ClaudeCodeCLI

    return ClaudeCodeCLI(config)
