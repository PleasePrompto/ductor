"""Welcome screen builder: text, auth status, quick-start keyboard."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.i18n import t
from ductor_bot.text.response_format import SEP

if TYPE_CHECKING:
    from ductor_bot.cli.auth import AuthResult
    from ductor_bot.config import AgentConfig

_WELCOME_PREFIX = "w:"

_CALLBACK_KEYS = ("w:1", "w:2", "w:3")


def _welcome_callbacks() -> dict[str, str]:
    return {
        "w:1": t("welcome.prompt_know"),
        "w:2": t("welcome.prompt_system"),
        "w:3": t("welcome.prompt_who"),
    }


def _button_labels_dict() -> dict[str, str]:
    return {
        "w:1": t("welcome.btn_know"),
        "w:2": t("welcome.btn_system"),
        "w:3": t("welcome.btn_who"),
    }


class _LazyDict(dict[str, str]):
    """Dict subclass that delegates to a builder function.

    Supports ``in``, iteration, ``keys()``, ``values()``, ``items()``
    and subscript access -- enough for production code and tests.
    """

    def __init__(self, builder: Callable[[], dict[str, str]]) -> None:
        super().__init__()
        self._builder = builder

    def _data(self) -> dict[str, str]:
        return self._builder()

    def __getitem__(self, key: str) -> str:
        return self._data()[key]

    def get(self, key: str, default: str | None = None) -> str | None:  # type: ignore[override]
        return self._data().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self._data()

    def __iter__(self) -> Iterator[str]:
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())


# Backward-compatible module-level names used by tests.
WELCOME_CALLBACKS: dict[str, str] = _LazyDict(_welcome_callbacks)
_BUTTON_LABELS: dict[str, str] = _LazyDict(_button_labels_dict)


def build_welcome_text(
    user_name: str,
    auth_results: dict[str, AuthResult],
    config: AgentConfig,
) -> str:
    """Build the welcome message with auth status block."""
    name = f", {user_name}" if user_name else ""

    auth_block = _build_auth_block(auth_results, config)

    return (
        f"{t('welcome.header', name=name)}\n\n"
        f"{t('welcome.tagline')}\n\n"
        f"{SEP}\n\n"
        f"{auth_block}\n\n"
        f"{SEP}\n\n"
        f"{t('welcome.hint_model')}\n"
        f"{t('welcome.hint_info')}\n"
        f"{t('welcome.hint_help')}"
    )


def build_welcome_keyboard() -> InlineKeyboardMarkup:
    """Build the 3 quick-start buttons."""
    labels = _button_labels_dict()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=key)] for key, label in labels.items()
        ],
    )


def is_welcome_callback(data: str) -> bool:
    """Check if callback data is a welcome quick-start button."""
    return data.startswith(_WELCOME_PREFIX)


def resolve_welcome_callback(data: str) -> str | None:
    """Map a welcome callback key to its full prompt text."""
    return _welcome_callbacks().get(data)


def get_welcome_button_label(data: str) -> str | None:
    """Return the display label for a welcome callback key."""
    return _button_labels_dict().get(data)


def _build_auth_block(auth_results: dict[str, AuthResult], config: AgentConfig) -> str:
    claude = auth_results.get("claude")
    codex = auth_results.get("codex")
    gemini = auth_results.get("gemini")
    grok = auth_results.get("grok")

    claude_ok = claude is not None and claude.is_authenticated
    codex_ok = codex is not None and codex.is_authenticated
    gemini_ok = gemini is not None and gemini.is_authenticated
    grok_ok = grok is not None and grok.is_authenticated

    providers: list[str] = []
    if claude_ok:
        providers.append("Claude Code")
    if codex_ok:
        providers.append("Codex")
    if gemini_ok:
        providers.append("Gemini")
    if grok_ok:
        providers.append("Grok Build")

    if not providers:
        return t("welcome.no_auth")

    providers_str = " + ".join(providers)
    model_name = config.model.capitalize() if config.provider == "claude" else config.model
    return t("welcome.auth_line", providers=providers_str, model=model_name)
