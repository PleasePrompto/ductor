"""Tests for TelegramTransport delivery handlers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.bus.cron_sanitize import (
    is_cron_transport_ack_line,
    sanitize_cron_result_text,
)
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.messenger.telegram.transport import TelegramTransport

if TYPE_CHECKING:
    import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_transport() -> tuple[TelegramTransport, MagicMock, AsyncMock]:
    """Build a TelegramTransport with mocked bot.

    Returns ``(transport, bot_mock, send_rich_mock)``.
    """
    bot = MagicMock()
    bot.bot_instance = MagicMock()
    bot.file_roots.return_value = [Path("/tmp/roots")]
    bot._orch.paths = MagicMock()
    bot._orch.named_sessions.update_after_response = MagicMock()
    bot.broadcast = AsyncMock()
    transport = TelegramTransport(bot)
    send_mock = AsyncMock()
    return transport, bot, send_mock


def _env(**kwargs: object) -> Envelope:
    defaults: dict[str, object] = {"origin": Origin.CRON, "chat_id": 42}
    defaults.update(kwargs)
    return Envelope(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cron sanitisation
# ---------------------------------------------------------------------------


class TestCronSanitisation:
    def test_ack_line_detected(self) -> None:
        line = 'Message sent successfully. "Hi" delivered to Telegram (id 5)'
        assert is_cron_transport_ack_line(line) is True

    def test_non_ack_line(self) -> None:
        assert is_cron_transport_ack_line("Hello world") is False

    def test_sanitize_strips_ack(self) -> None:
        raw = 'Useful output\nMessage sent successfully. "Hi" delivered to Telegram (id 5).'
        assert sanitize_cron_result_text(raw) == "Useful output"

    def test_sanitize_empty(self) -> None:
        assert sanitize_cron_result_text("") == ""

    def test_sanitize_ack_only(self) -> None:
        raw = 'Message sent successfully. "Hi" delivered to Telegram (id 5).'
        assert sanitize_cron_result_text(raw) == ""


# ---------------------------------------------------------------------------
# Cron broadcast
# ---------------------------------------------------------------------------


class TestCronBroadcast:
    async def test_broadcasts_with_result_text(self) -> None:
        transport, bot, _ = _make_transport()
        env = _env(
            origin=Origin.CRON,
            result_text="All good",
            status="success",
            metadata={"title": "Backup"},
        )

        with patch("ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock):
            await transport.deliver_broadcast(env)

        bot.broadcast.assert_awaited_once()
        text = bot.broadcast.call_args[0][0]
        assert "**TASK: Backup**" in text
        assert "All good" in text

    async def test_broadcasts_status_only_when_no_text(self) -> None:
        transport, bot, _ = _make_transport()
        env = _env(
            origin=Origin.CRON,
            result_text="",
            status="failed",
            metadata={"title": "Deploy"},
        )

        await transport.deliver_broadcast(env)

        text = bot.broadcast.call_args[0][0]
        assert "**TASK: Deploy**" in text
        assert "_failed_" in text

    async def test_skips_ack_only_success(self) -> None:
        transport, bot, _ = _make_transport()
        env = _env(
            origin=Origin.CRON,
            result_text='Message sent successfully. "Hi" delivered to Telegram (id 5).',
            status="success",
            metadata={"title": "Greet"},
        )

        await transport.deliver_broadcast(env)

        bot.broadcast.assert_not_awaited()


# ---------------------------------------------------------------------------
# Heartbeat delivery
# ---------------------------------------------------------------------------


class TestHeartbeatDelivery:
    async def test_delivers_to_chat(self) -> None:
        transport, _bot, _ = _make_transport()
        env = _env(
            origin=Origin.HEARTBEAT,
            chat_id=99,
            result_text="Something happened",
        )

        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        mock_send.assert_awaited_once()
        assert mock_send.call_args[0][1] == 99
        assert mock_send.call_args[0][2] == "Something happened"


# ---------------------------------------------------------------------------
# Webhook cron broadcast
# ---------------------------------------------------------------------------


class TestWebhookCronBroadcast:
    async def test_broadcasts_with_text(self) -> None:
        transport, bot, _ = _make_transport()
        env = _env(
            origin=Origin.WEBHOOK_CRON,
            result_text="Deployed v3",
            status="success",
            metadata={"hook_title": "Deploy"},
        )

        await transport.deliver_broadcast(env)

        text = bot.broadcast.call_args[0][0]
        assert "WEBHOOK (CRON TASK)" in text
        assert "Deploy" in text
        assert "Deployed v3" in text

    async def test_broadcasts_status_when_no_text(self) -> None:
        transport, bot, _ = _make_transport()
        env = _env(
            origin=Origin.WEBHOOK_CRON,
            result_text="",
            status="error",
            metadata={"hook_title": "Hook"},
        )

        await transport.deliver_broadcast(env)

        text = bot.broadcast.call_args[0][0]
        assert "_error_" in text


# ---------------------------------------------------------------------------
# Background delivery
# ---------------------------------------------------------------------------


class TestBackgroundDelivery:
    async def test_named_session_success(self) -> None:
        transport, bot, _ = _make_transport()
        env = _env(
            origin=Origin.BACKGROUND,
            session_name="research",
            session_id="sid-1",
            result_text="Found results",
            status="success",
            elapsed_seconds=12.5,
        )

        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        bot._orch.named_sessions.update_after_response.assert_called_once_with(
            42, "research", "sid-1"
        )
        text = mock_send.call_args[0][2]
        assert "[research] Complete" in text
        assert "12s" in text

    async def test_named_session_error(self) -> None:
        transport, _, _ = _make_transport()
        env = _env(
            origin=Origin.BACKGROUND,
            session_name="debug",
            result_text="CLI crash",
            status="error:timeout",
            is_error=True,
            elapsed_seconds=60.0,
        )

        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        text = mock_send.call_args[0][2]
        assert "[debug] Failed" in text
        assert "CLI crash" in text

    async def test_named_session_aborted(self) -> None:
        transport, _, _ = _make_transport()
        env = _env(
            origin=Origin.BACKGROUND,
            session_name="task1",
            prompt_preview="do stuff",
            status="aborted",
            elapsed_seconds=0.0,
        )

        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        text = mock_send.call_args[0][2]
        assert "[task1] Cancelled" in text

    async def test_stateless_success(self) -> None:
        transport, _, _ = _make_transport()
        env = _env(
            origin=Origin.BACKGROUND,
            result_text="Done",
            status="success",
            elapsed_seconds=5.0,
            metadata={"task_id": "abc123"},
        )

        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        text = mock_send.call_args[0][2]
        assert "Background Task Complete" in text
        assert "Done" in text

    async def test_stateless_error(self) -> None:
        transport, _, _ = _make_transport()
        env = _env(
            origin=Origin.BACKGROUND,
            result_text="Timeout",
            status="error:timeout",
            is_error=True,
            elapsed_seconds=120.0,
            prompt_preview="run tests",
            metadata={"task_id": "xyz"},
        )

        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        text = mock_send.call_args[0][2]
        assert "Background Task Failed" in text
        assert "xyz" in text

    async def test_stateless_aborted(self) -> None:
        transport, _, _ = _make_transport()
        env = _env(
            origin=Origin.BACKGROUND,
            status="aborted",
            prompt_preview="fix bug",
            metadata={"task_id": "t1"},
        )

        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        text = mock_send.call_args[0][2]
        assert "Background Task Cancelled" in text
        assert "t1" in text


# ---------------------------------------------------------------------------
# Inter-agent delivery
# ---------------------------------------------------------------------------


class TestInteragentDelivery:
    async def test_error_sends_notification(self) -> None:
        transport, _, _ = _make_transport()
        env = _env(
            origin=Origin.INTERAGENT,
            is_error=True,
            prompt_preview="translate this",
            metadata={"recipient": "sub-agent", "error": "timeout"},
        )

        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        mock_send.assert_awaited_once()
        text = mock_send.call_args[0][2]
        assert "Inter-Agent Request Failed" in text
        assert "sub-agent" in text
        assert "timeout" in text

    async def test_success_with_provider_switch(self) -> None:
        transport, _, _ = _make_transport()
        env = _env(
            origin=Origin.INTERAGENT,
            result_text="Response text",
            metadata={"provider_switch_notice": "Switched to gemini"},
        )

        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        assert mock_send.await_count == 2
        first_text = mock_send.call_args_list[0][0][2]
        assert "Provider Switch Detected" in first_text
        second_text = mock_send.call_args_list[1][0][2]
        assert second_text == "Response text"


# ---------------------------------------------------------------------------
# Task result delivery
# ---------------------------------------------------------------------------


class TestDispatchFallback:
    async def test_unknown_origin_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        transport, _, _ = _make_transport()
        # Use an origin that's not in _HANDLERS for unicast
        env = _env(origin=Origin.USER)  # USER has no delivery handler

        with patch("ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock):
            await transport.deliver(env)

        assert "No handler for origin=user" in caplog.text
