"""TOML-based translation loader with fallback chain."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

_I18N_DIR = Path(__file__).resolve().parent


def _flatten(data: dict[str, object], prefix: str = "") -> dict[str, str]:
    """Flatten nested TOML dict into dotted keys."""
    flat: dict[str, str] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, full_key))
        elif isinstance(value, str):
            flat[full_key] = value
        else:
            flat[full_key] = str(value)
    return flat


def _load_toml(path: Path) -> dict[str, str]:
    """Load a single TOML file and return flattened keys."""
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
        return _flatten(data)
    except FileNotFoundError:
        logger.debug("Translation file not found: %s", path)
        return {}
    except tomllib.TOMLDecodeError:
        logger.warning("Failed to parse translation file: %s", path)
        return {}


def _load_language(lang: str) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Load all TOML files for a language.

    Returns (chat_keys, cli_keys, cmd_keys).
    CLI keys merge cli.toml + wizard.toml into one namespace.
    """
    lang_dir = _I18N_DIR / lang
    chat = _load_toml(lang_dir / "chat.toml")
    cli = _load_toml(lang_dir / "cli.toml")
    wizard = _load_toml(lang_dir / "wizard.toml")
    # Wizard keys go into cli namespace (both Rich-formatted).
    # Prefix wizard keys so they don't collide.
    for k, v in wizard.items():
        cli[f"wizard.{k}"] = v
    cmd = _load_toml(lang_dir / "commands.toml")
    return chat, cli, cmd


class TranslationStore:
    """Holds translations for one language with English fallback."""

    def __init__(self, language: str) -> None:
        self.language = language
        # Always load English as fallback.
        self._en_chat, self._en_cli, self._en_cmd = _load_language("en")
        if language == "en":
            self._chat = self._en_chat
            self._cli = self._en_cli
            self._cmd = self._en_cmd
        else:
            self._chat, self._cli, self._cmd = _load_language(language)

    def chat(self, key: str, **kwargs: object) -> str:
        """Look up a chat/Markdown string."""
        return self._resolve(self._chat, self._en_chat, key, kwargs)

    def cli(self, key: str, **kwargs: object) -> str:
        """Look up a CLI/Rich string."""
        return self._resolve(self._cli, self._en_cli, key, kwargs)

    def cmd(self, key: str) -> str:
        """Look up a bot command description."""
        raw = self._cmd.get(key) or self._en_cmd.get(key)
        if raw is None:
            logger.warning("Missing translation key: commands.%s", key)
            return f"[MISSING: {key}]"
        return raw

    def _resolve(
        self,
        primary: dict[str, str],
        fallback: dict[str, str],
        key: str,
        kwargs: dict[str, object],
    ) -> str:
        raw = primary.get(key) or fallback.get(key)
        if raw is None:
            logger.warning("Missing translation key: %s", key)
            return f"[MISSING: {key}]"
        if kwargs:
            try:
                return raw.format_map({k: str(v) for k, v in kwargs.items()})
            except KeyError as exc:
                logger.warning("Translation key %s: missing placeholder %s", key, exc)
                return raw
        return raw
