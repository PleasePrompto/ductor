"""Tests for startup and recovery message formatters."""

from __future__ import annotations

from ductor_bot.text.response_format import (
    recovery_notification_text,
    startup_notification_text,
)


class TestStartupNotificationText:
    def test_first_start(self) -> None:
        result = startup_notification_text("first_start")
        assert "First start" in result

    def test_system_reboot(self) -> None:
        result = startup_notification_text("system_reboot")
        assert "reboot" in result.lower()

    def test_service_restart_silent(self) -> None:
        result = startup_notification_text("service_restart")
        assert result == ""


class TestRecoveryNotificationText:
    def test_foreground(self) -> None:
        result = recovery_notification_text("foreground", "fix the login bug")
        assert "fix the login bug" in result
        assert "Interrupted" in result

    def test_named_session(self) -> None:
        result = recovery_notification_text("named_session", "deploy stuff", "boldowl")
        assert "boldowl" in result
        assert "deploy stuff" in result

    def test_long_preview_truncated(self) -> None:
        long_prompt = "x" * 200
        result = recovery_notification_text("foreground", long_prompt)
        assert "…" in result
        assert len(long_prompt) > 80  # confirm original was long
