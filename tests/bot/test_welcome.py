"""Tests for welcome screen builder: text, auth status, keyboard, callbacks."""

from __future__ import annotations

import pytest

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.config import AgentConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(provider: str, *, authenticated: bool = True) -> AuthResult:
    """Build an AuthResult with the given auth state."""
    status = AuthStatus.AUTHENTICATED if authenticated else AuthStatus.NOT_FOUND
    return AuthResult(provider=provider, status=status)


def _config(**overrides: object) -> AgentConfig:
    """Build an AgentConfig with sensible defaults, applying *overrides*."""
    defaults: dict[str, object] = {
        "telegram_token": "test:token",
        "allowed_user_ids": [1],
        "provider": "claude",
        "model": "opus",
        "reasoning_effort": "medium",
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# build_welcome_text
# ---------------------------------------------------------------------------


class TestBuildWelcomeText:
    def test_both_providers_authenticated(self) -> None:
        from ductor_bot.bot.welcome import build_welcome_text

        auth_results = {
            "claude": _auth("claude"),
            "codex": _auth("codex"),
        }
        text = build_welcome_text("Alice", auth_results, _config(model="sonnet"))

        assert "Welcome to ductor.dev, Alice!" in text
        assert "Claude Code + Codex authenticated" in text
        assert "Sonnet" in text

    def test_only_claude_authenticated(self) -> None:
        from ductor_bot.bot.welcome import build_welcome_text

        auth_results = {
            "claude": _auth("claude"),
            "codex": _auth("codex", authenticated=False),
        }
        text = build_welcome_text("Bob", auth_results, _config(model="haiku"))

        assert "Claude Code authenticated" in text
        assert "Haiku" in text
        assert "Codex authenticated" not in text

    def test_only_codex_authenticated(self) -> None:
        from ductor_bot.bot.welcome import build_welcome_text

        auth_results = {
            "claude": _auth("claude", authenticated=False),
            "codex": _auth("codex"),
        }
        cfg = _config(model="gpt-5.2-codex", provider="codex", reasoning_effort="high")
        text = build_welcome_text("Carol", auth_results, cfg)

        assert "Codex authenticated" in text
        assert "gpt-5.2-codex" in text

    def test_no_providers_authenticated(self) -> None:
        from ductor_bot.bot.welcome import build_welcome_text

        auth_results = {
            "claude": _auth("claude", authenticated=False),
            "codex": _auth("codex", authenticated=False),
        }
        text = build_welcome_text("Dave", auth_results, _config())

        assert "No CLI authenticated" in text
        assert "claude auth" in text
        assert "codex auth" in text

    def test_empty_auth_results(self) -> None:
        from ductor_bot.bot.welcome import build_welcome_text

        text = build_welcome_text("Eve", {}, _config())

        assert "No CLI authenticated" in text

    def test_user_name_present(self) -> None:
        from ductor_bot.bot.welcome import build_welcome_text

        text = build_welcome_text("Zara", {}, _config())
        assert "Welcome to ductor.dev, Zara!" in text

    def test_user_name_empty(self) -> None:
        from ductor_bot.bot.welcome import build_welcome_text

        text = build_welcome_text("", {}, _config())
        assert "Welcome to ductor.dev!" in text
        assert "Welcome to ductor.dev, " not in text

    def test_static_content_present(self) -> None:
        from ductor_bot.bot.welcome import build_welcome_text

        text = build_welcome_text("X", {}, _config())

        assert "Deploy from your pocket" in text
        assert "Claude Code" in text
        assert "/model" in text
        assert "/help" in text
        assert "/info" in text

    @pytest.mark.parametrize(
        ("model", "expected_fragment"),
        [
            ("opus", "Opus"),
            ("sonnet", "Sonnet"),
            ("haiku", "Haiku"),
        ],
    )
    def test_model_capitalized_in_claude_auth_block(
        self,
        model: str,
        expected_fragment: str,
    ) -> None:
        from ductor_bot.bot.welcome import build_welcome_text

        auth_results = {"claude": _auth("claude")}
        text = build_welcome_text("U", auth_results, _config(model=model))

        assert expected_fragment in text


# ---------------------------------------------------------------------------
# _build_auth_block (internal, tested indirectly via build_welcome_text above
# and directly below for targeted coverage)
# ---------------------------------------------------------------------------


class TestBuildAuthBlock:
    def test_both_ok_mentions_both_providers(self) -> None:
        from ductor_bot.bot.welcome import _build_auth_block

        auth_results = {
            "claude": _auth("claude"),
            "codex": _auth("codex"),
        }
        block = _build_auth_block(auth_results, _config(model="opus"))

        assert "Claude Code + Codex" in block
        assert "Opus" in block

    def test_codex_only_shows_model(self) -> None:
        from ductor_bot.bot.welcome import _build_auth_block

        auth_results = {
            "claude": _auth("claude", authenticated=False),
            "codex": _auth("codex"),
        }
        cfg = _config(model="gpt-5.1-codex-mini", provider="codex", reasoning_effort="low")
        block = _build_auth_block(auth_results, cfg)

        assert "Codex authenticated" in block
        assert "gpt-5.1-codex-mini" in block

    def test_claude_missing_from_dict(self) -> None:
        from ductor_bot.bot.welcome import _build_auth_block

        auth_results: dict[str, AuthResult] = {"codex": _auth("codex")}
        block = _build_auth_block(auth_results, _config(provider="codex"))

        assert "Codex authenticated" in block

    def test_codex_missing_from_dict(self) -> None:
        from ductor_bot.bot.welcome import _build_auth_block

        auth_results: dict[str, AuthResult] = {"claude": _auth("claude")}
        block = _build_auth_block(auth_results, _config(model="sonnet"))

        assert "Claude Code authenticated" in block
        assert "Sonnet" in block


# ---------------------------------------------------------------------------
# build_welcome_keyboard
# ---------------------------------------------------------------------------


class TestBuildWelcomeKeyboard:
    def test_returns_inline_keyboard_markup(self) -> None:
        from aiogram.types import InlineKeyboardMarkup

        from ductor_bot.bot.welcome import build_welcome_keyboard

        kb = build_welcome_keyboard()
        assert isinstance(kb, InlineKeyboardMarkup)

    def test_has_three_rows(self) -> None:
        from ductor_bot.bot.welcome import build_welcome_keyboard

        kb = build_welcome_keyboard()
        assert len(kb.inline_keyboard) == 3

    def test_each_row_has_one_button(self) -> None:
        from ductor_bot.bot.welcome import build_welcome_keyboard

        kb = build_welcome_keyboard()
        for row in kb.inline_keyboard:
            assert len(row) == 1

    def test_callback_data_matches_welcome_keys(self) -> None:
        from ductor_bot.bot.welcome import WELCOME_CALLBACKS, build_welcome_keyboard

        kb = build_welcome_keyboard()
        callback_keys = [row[0].callback_data for row in kb.inline_keyboard]

        for key in callback_keys:
            assert key in WELCOME_CALLBACKS

    def test_button_labels_match_expected(self) -> None:
        from ductor_bot.bot.welcome import _BUTTON_LABELS, build_welcome_keyboard

        kb = build_welcome_keyboard()

        for row in kb.inline_keyboard:
            btn = row[0]
            assert btn.callback_data is not None
            assert btn.text == _BUTTON_LABELS[btn.callback_data]


# ---------------------------------------------------------------------------
# is_welcome_callback
# ---------------------------------------------------------------------------


class TestIsWelcomeCallback:
    @pytest.mark.parametrize("data", ["w:1", "w:2", "w:3"])
    def test_valid_welcome_data(self, data: str) -> None:
        from ductor_bot.bot.welcome import is_welcome_callback

        assert is_welcome_callback(data) is True

    @pytest.mark.parametrize("data", ["w:999", "w:"])
    def test_valid_prefix_unknown_key(self, data: str) -> None:
        from ductor_bot.bot.welcome import is_welcome_callback

        assert is_welcome_callback(data) is True

    @pytest.mark.parametrize(
        "data",
        [
            "ms:p:claude",
            "Yes",
            "",
            "welcome:1",
            "W:1",
            "x:1",
        ],
    )
    def test_non_welcome_data(self, data: str) -> None:
        from ductor_bot.bot.welcome import is_welcome_callback

        assert is_welcome_callback(data) is False


# ---------------------------------------------------------------------------
# resolve_welcome_callback
# ---------------------------------------------------------------------------


class TestResolveWelcomeCallback:
    @pytest.mark.parametrize(
        ("key", "expected_substring"),
        [
            ("w:1", "ductor.dev"),
            ("w:2", "system"),
            ("w:3", "introduce"),
        ],
    )
    def test_known_keys_return_prompt(self, key: str, expected_substring: str) -> None:
        from ductor_bot.bot.welcome import resolve_welcome_callback

        result = resolve_welcome_callback(key)
        assert result is not None
        assert expected_substring in result.lower()

    def test_unknown_key_returns_none(self) -> None:
        from ductor_bot.bot.welcome import resolve_welcome_callback

        assert resolve_welcome_callback("w:99") is None

    def test_non_welcome_key_returns_none(self) -> None:
        from ductor_bot.bot.welcome import resolve_welcome_callback

        assert resolve_welcome_callback("ms:p:claude") is None

    def test_empty_string_returns_none(self) -> None:
        from ductor_bot.bot.welcome import resolve_welcome_callback

        assert resolve_welcome_callback("") is None

    def test_resolved_prompts_are_non_empty_strings(self) -> None:
        from ductor_bot.bot.welcome import WELCOME_CALLBACKS, resolve_welcome_callback

        for key in WELCOME_CALLBACKS:
            result = resolve_welcome_callback(key)
            assert isinstance(result, str)
            assert len(result) > 0
