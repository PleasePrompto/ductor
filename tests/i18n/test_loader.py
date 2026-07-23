"""Tests for the i18n translation loader."""

from __future__ import annotations

import re
from pathlib import Path

from ductor_bot.i18n import LANGUAGES, init, t, t_cmd, t_plural, t_rich
from ductor_bot.i18n.loader import TranslationStore, _flatten, _load_toml

_I18N_ROOT = Path(__file__).resolve().parent.parent.parent / "ductor_bot" / "i18n"


def _en_chat_keys() -> set[str]:
    return set(_load_toml(_I18N_ROOT / "en" / "chat.toml"))


def _en_cmd_keys() -> set[str]:
    return set(_load_toml(_I18N_ROOT / "en" / "commands.toml"))


# -- _flatten ------------------------------------------------------------------


def test_flatten_simple() -> None:
    assert _flatten({"a": "hello"}) == {"a": "hello"}


def test_flatten_nested() -> None:
    assert _flatten({"a": {"b": "hello", "c": "world"}}) == {
        "a.b": "hello",
        "a.c": "world",
    }


def test_flatten_deep() -> None:
    result = _flatten({"a": {"b": {"c": "deep"}}})
    assert result == {"a.b.c": "deep"}


# -- _load_toml ----------------------------------------------------------------


def test_load_toml_missing(tmp_path: Path) -> None:
    result = _load_toml(tmp_path / "nonexistent.toml")
    assert result == {}


def test_load_toml_invalid(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("this is not [valid toml", encoding="utf-8")
    result = _load_toml(bad)
    assert result == {}


def test_load_toml_valid(tmp_path: Path) -> None:
    good = tmp_path / "good.toml"
    good.write_text('[section]\nkey = "value"', encoding="utf-8")
    result = _load_toml(good)
    assert result == {"section.key": "value"}


# -- TranslationStore ---------------------------------------------------------


def test_store_english() -> None:
    store = TranslationStore("en")
    assert store.language == "en"
    # chat.toml must have at least some keys.
    assert len(_en_chat_keys()) > 0
    assert len(_en_cmd_keys()) > 0


def test_store_fallback_missing_key() -> None:
    store = TranslationStore("en")
    result = store.chat("this.key.does.not.exist")
    assert result == "[MISSING: this.key.does.not.exist]"


def test_store_variable_substitution() -> None:
    store = TranslationStore("en")
    result = store.chat("session.error_body", model="opus")
    assert "opus" in result
    assert "{model}" not in result


def test_store_missing_placeholder_graceful() -> None:
    store = TranslationStore("en")
    # Call with a key that has placeholders but don't provide them.
    result = store.chat("session.error_body")
    # Should return raw string (not crash), since format_map raises KeyError.
    assert "{model}" in result


# -- Public API ----------------------------------------------------------------


def test_init_default() -> None:
    init()
    assert "Session Error" in t("session.error_header")


def test_init_english() -> None:
    init("en")
    assert "Session Error" in t("session.error_header")


def test_init_unknown_falls_back_to_english() -> None:
    init("xx_unknown")
    assert "Session Error" in t("session.error_header")


def test_t_returns_string() -> None:
    init("en")
    result = t("session.error_header")
    assert isinstance(result, str)
    assert "Session Error" in result


def test_t_with_kwargs() -> None:
    init("en")
    result = t("stop.killed", provider="Claude")
    assert "Claude" in result


def test_t_rich_returns_string() -> None:
    init("en")
    result = t_rich("wizard.common.cancelled")
    assert isinstance(result, str)
    assert "cancelled" in result.lower()


def test_t_cmd_returns_string() -> None:
    init("en")
    result = t_cmd("bot.new")
    assert isinstance(result, str)
    assert len(result) > 0


def test_t_plural_one() -> None:
    init("en")
    result = t_plural("tasks.cancelled", 1)
    assert "1 task." in result


def test_t_plural_many() -> None:
    init("en")
    result = t_plural("tasks.cancelled", 5)
    assert "5 tasks." in result


# -- TOML file integrity -------------------------------------------------------


def test_all_chat_keys_resolvable() -> None:
    """Every English chat key should resolve without error."""
    init("en")
    for key in _en_chat_keys():
        result = t(key)
        assert "[MISSING:" not in result, f"Key {key!r} is missing"


def test_all_cmd_keys_resolvable() -> None:
    init("en")
    for key in _en_cmd_keys():
        result = t_cmd(key)
        assert "[MISSING:" not in result, f"Key {key!r} is missing"


def test_no_empty_values() -> None:
    """No chat key should have an empty string value."""
    init("en")
    for key in _en_chat_keys():
        result = t(key)
        # Skip keys that are legitimately short.
        assert result.strip(), f"Key {key!r} has empty value"


def test_command_descriptions_short() -> None:
    """Bot command descriptions must fit Telegram's ≤256 char limit."""
    init("en")
    for key in _en_cmd_keys():
        val = t_cmd(key)
        assert len(val) <= 256, f"Command {key!r} too long: {len(val)} chars"


# -- Placeholder consistency ---------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _extract_placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(text))


def test_chat_placeholders_are_valid() -> None:
    """All placeholders in chat strings should be simple {word} format."""
    init("en")
    for key in _en_chat_keys():
        val = t(key)
        placeholders = _extract_placeholders(val)
        for ph in placeholders:
            assert ph.isidentifier(), f"Bad placeholder {{{ph}}} in {key}"


# -- LANGUAGES dict consistency ------------------------------------------------


def test_languages_has_en() -> None:
    assert "en" in LANGUAGES


def test_all_language_dirs_exist() -> None:
    for lang_code in LANGUAGES:
        lang_dir = _I18N_ROOT / lang_code
        assert lang_dir.is_dir(), f"Language dir missing: {lang_dir}"
