"""Welcome screen builder: text, auth status, quick-start keyboard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.bot.response_format import SEP

if TYPE_CHECKING:
    from ductor_bot.cli.auth import AuthResult
    from ductor_bot.config import AgentConfig

_WELCOME_PREFIX = "w:"

WELCOME_CALLBACKS: dict[str, str] = {
    "w:1": (
        "Hey, I just set up ductor.dev and I want you to get to know me. "
        "Ask me everything you need to know so we can work well together -- "
        "my name, what I do, what I'm working on, how I like to communicate. "
        "Save what you learn to your memory."
    ),
    "w:2": (
        "Take a look around the system you're running on. "
        "Check the OS, installed tools, project folders, whatever you find interesting. "
        "Give me a quick summary of what you see."
    ),
    "w:3": (
        "Let's get started! Introduce yourself -- who are you, what can you do for me? "
        "Then ask me who I am and what I need help with."
    ),
}

_BUTTON_LABELS: dict[str, str] = {
    "w:1": "Let's get to know each other!",
    "w:2": "Check out the system!",
    "w:3": "Who are you? Who am I?",
}


def build_welcome_text(
    user_name: str,
    auth_results: dict[str, AuthResult],
    config: AgentConfig,
) -> str:
    """Build the welcome message with auth status block."""
    name = f", {user_name}" if user_name else ""

    auth_block = _build_auth_block(auth_results, config)

    return (
        f"**Welcome to ductor.dev{name}!**\n\n"
        "Deploy from your pocket. Automate from your couch.\n"
        "Claude Code & OpenAI Codex -- straight from Telegram.\n\n"
        f"{SEP}\n\n"
        f"{auth_block}\n\n"
        f"{SEP}\n\n"
        "/model \u2014 switch models\n"
        "/info \u2014 docs & links\n"
        "/help \u2014 all commands"
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

    providers: list[str] = []
    if claude_ok:
        providers.append("Claude Code")
    if codex_ok:
        providers.append("Codex")

    if not providers:
        return "No CLI authenticated yet. Run `claude auth` or `codex auth` to get started."

    auth_line = " + ".join(providers) + " authenticated."
    model_name = config.model.capitalize() if config.provider == "claude" else config.model
    return f"{auth_line}\nModel: **{model_name}**"
