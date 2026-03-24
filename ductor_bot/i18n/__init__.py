"""Internationalization module for ductor-bot.

Public API::

    from ductor_bot.i18n import init, t, t_rich, t_cmd, t_plural

    init("de")  # once at startup
    t("session.error", model="opus")  # chat/Markdown string
    t_rich("lifecycle.stopped")  # CLI/Rich string
    t_cmd("new")  # bot command description
    t_plural("tasks.cancelled", 3, count=3)  # plural-aware
"""

from __future__ import annotations

import logging

from ductor_bot.i18n.loader import TranslationStore

logger = logging.getLogger(__name__)

_store: TranslationStore | None = None

# Available languages: directory name -> display name (native).
LANGUAGES: dict[str, str] = {
    "en": "English",
    "de": "Deutsch",
    "nl": "Nederlands",
    "es": "Español",
    "fr": "Français",
    "id": "Bahasa Indonesia",
    "pt": "Português",
    "ru": "Русский",
}


def init(language: str = "en") -> None:
    """Initialize the translation store. Call once at startup."""
    global _store  # noqa: PLW0603
    lang = language if language in LANGUAGES else "en"
    if lang != language:
        logger.warning("Unknown language '%s', falling back to 'en'", language)
    _store = TranslationStore(lang)
    logger.info("i18n initialized: language=%s", lang)


def _get_store() -> TranslationStore:
    if _store is None:
        # Auto-init with English if nobody called init() yet.
        init("en")
        assert _store is not None
    return _store


def t(key: str, **kwargs: object) -> str:
    """Translate a chat/Markdown string with variable substitution."""
    return _get_store().chat(key, **kwargs)


def t_rich(key: str, **kwargs: object) -> str:
    """Translate a CLI/Rich string with variable substitution."""
    return _get_store().cli(key, **kwargs)


def t_cmd(key: str) -> str:
    """Translate a bot command description."""
    return _get_store().cmd(key)


def t_plural(key: str, count: int, **kwargs: object) -> str:
    """Translate with simple plural rules (_one / _other suffix)."""
    suffix = "_one" if count == 1 else "_other"
    return t(f"{key}{suffix}", count=count, **kwargs)


def get_language() -> str:
    """Return the active language code."""
    return _get_store().language


def get_store() -> TranslationStore:
    """Return the active TranslationStore (for validation/testing)."""
    return _get_store()
