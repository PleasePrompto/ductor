"""Tests for timeout, startup, and recovery message formatters."""

from __future__ import annotations

from ductor_bot.text.response_format import (
    recovery_notification_text,
    startup_notification_text,
    timeout_extended_text,
    timeout_result_text,
    timeout_warning_text,
)


class TestTimeoutWarningText:
    def test_minutes(self) -> None:
        result = timeout_warning_text(120.0)
        assert "2 min" in result

    def test_seconds(self) -> None:
        result = timeout_warning_text(30.0)
        assert "30s" in result

    def test_boundary_60(self) -> None:
        result = timeout_warning_text(60.0)
        assert "1 min" in result


class TestTimeoutExtendedText:
    def test_format(self) -> None:
        result = timeout_extended_text(120.0, 2)
        assert "+120s" in result
        assert "2 left" in result


class TestTimeoutResultText:
    def test_contains_times(self) -> None:
        result = timeout_result_text(600.0, 600.0)
        assert "600" in result
        assert "Timeout" in result


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
