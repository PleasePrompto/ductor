"""Tests for response_format helpers."""

from __future__ import annotations

from ductor_bot.text.response_format import classify_cli_error, session_error_text


class TestClassifyCliError:
    def test_401_unauthorized(self) -> None:
        assert "Authentication" in (classify_cli_error("401 Unauthorized: bad token") or "")

    def test_token_invalidated(self) -> None:
        result = classify_cli_error("Your authentication token has been invalidated")
        assert result is not None
        assert "re-authenticate" in result

    def test_sign_in_again(self) -> None:
        result = classify_cli_error("Please try signing in again.")
        assert result is not None
        assert "Authentication" in result

    def test_rate_limit(self) -> None:
        result = classify_cli_error("429 Too Many Requests")
        assert result is not None
        assert "Rate limit" in result

    def test_quota_exceeded(self) -> None:
        result = classify_cli_error("quota exceeded for model")
        assert result is not None
        assert "Rate limit" in result

    def test_codex_usage_limit(self) -> None:
        """Issue #117: Codex says 'usage limit', not 'rate limit' / '429'."""
        result = classify_cli_error("You've hit your usage limit. Try again at 6:00 PM.")
        assert result is not None
        assert "Rate limit" in result

    def test_codex_upgrade_to_pro(self) -> None:
        result = classify_cli_error("Upgrade to Pro for higher limits.")
        assert result is not None
        assert "Rate limit" in result

    def test_codex_hit_your(self) -> None:
        """'hit your' covers 'hit your usage limit', 'hit your monthly cap', etc."""
        result = classify_cli_error("you have hit your monthly cap")
        assert result is not None
        assert "Rate limit" in result

    def test_context_length(self) -> None:
        result = classify_cli_error("maximum context length exceeded")
        assert result is not None
        assert "/new" in result

    def test_unknown_error(self) -> None:
        assert classify_cli_error("something random broke") is None

    def test_empty_string(self) -> None:
        assert classify_cli_error("") is None


class TestSessionErrorText:
    def test_with_auth_error(self) -> None:
        text = session_error_text("codex", "401 Unauthorized: bad token")
        assert "Session Error" in text
        assert "[codex]" in text
        assert "Authentication failed" in text

    def test_with_unknown_error(self) -> None:
        text = session_error_text("opus", "Something weird happened\nMore details")
        assert "Session Error" in text
        assert "Something weird happened" in text
        assert "More details" not in text

    def test_without_detail(self) -> None:
        text = session_error_text("opus")
        assert "Session Error" in text
        assert "Cause" not in text
        assert "Detail" not in text

    def test_with_empty_detail(self) -> None:
        text = session_error_text("opus", "")
        assert "Session Error" in text
        assert "Cause" not in text

