"""Welcome screen builder: text, auth status, quick-start keyboard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

if TYPE_CHECKING:
    from ductor_bot.cli.auth import AuthResult
    from ductor_bot.config import AgentConfig

_WELCOME_PREFIX = "w:"

WELCOME_CALLBACKS: dict[str, str] = {
    "w:1": (
        "Hey, I just set up ductor.dev. What do you need to know about me so we can get started?"
    ),
    "w:2": "What can you do? Walk me through your capabilities!",
    "w:3": "Give me a tour of the system you're running on!",
}

_BUTTON_LABELS: dict[str, str] = {
    "w:1": "What do you need to know about me?",
    "w:2": "Show me what you can do!",
    "w:3": "Give me a system tour!",
}


def build_welcome_text(
    user_name: str,
    auth_results: dict[str, AuthResult],
    config: AgentConfig,
) -> str:
    """Build the welcome message with auth status block."""
    greeting = f"Welcome to ductor.dev, {user_name}!" if user_name else "Welcome to ductor.dev!"

    auth_block = _build_auth_block(auth_results, config)

    return (
        f"{greeting}\n\n"
        "Deploy from your pocket. Automate recurring tasks.\n"
        "Powered by Claude Code and OpenAI Codex -- right from Telegram.\n\n"
        f"{auth_block}\n\n"
        "/model -- switch models | /help -- all commands\n\n"
        "Let's go!"
    )


def build_welcome_keyboard() -> InlineKeyboardMarkup:
    """Build the 3 quick-start buttons."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=key)]
            for key, label in _BUTTON_LABELS.items()
        ],
    )


def is_welcome_callback(data: str) -> bool:
    """Check if callback data is a welcome quick-start button."""
    return data.startswith(_WELCOME_PREFIX)


def resolve_welcome_callback(data: str) -> str | None:
    """Map a welcome callback key to its full prompt text."""
    return WELCOME_CALLBACKS.get(data)


def get_welcome_button_label(data: str) -> str | None:
    """Return the display label for a welcome callback key."""
    return _BUTTON_LABELS.get(data)


def _build_auth_block(auth_results: dict[str, AuthResult], config: AgentConfig) -> str:
    claude = auth_results.get("claude")
    codex = auth_results.get("codex")

    claude_ok = claude is not None and claude.is_authenticated
    codex_ok = codex is not None and codex.is_authenticated

    if claude_ok and codex_ok:
        return (
            "Claude Code + Codex are authenticated.\n"
            f"Default model: Claude {config.model.capitalize()}."
        )
    if claude_ok:
        return f"Claude Code is authenticated.\nDefault model: {config.model.capitalize()}."
    if codex_ok:
        return (
            f"Codex is authenticated.\nDefault model: {config.model} ({config.reasoning_effort})."
        )
    return "No CLI provider authenticated. Run `claude auth` or `codex auth` to get started."
