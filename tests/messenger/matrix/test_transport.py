"""Tests for MatrixTransport delivery handlers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.messenger.matrix.transport import MatrixTransport

if TYPE_CHECKING:
    import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_transport() -> tuple[MatrixTransport, MagicMock]:
    """Build a MatrixTransport with mocked bot.

    Returns ``(transport, bot_mock)``.
    """
    bot = MagicMock()
    bot.client = MagicMock()
    bot.config.matrix.allowed_rooms = ["!room1:test"]
    bot._last_active_room = "!room1:test"
    bot.orchestrator.paths = MagicMock()
    bot.file_roots.return_value = [Path("/tmp/roots")]
    bot.orchestrator = MagicMock()
    transport = MatrixTransport(bot)
    return transport, bot


def _env(**kwargs: object) -> Envelope:
    defaults: dict[str, object] = {"origin": Origin.CRON, "chat_id": "42"}
    defaults.update(kwargs)
    return Envelope(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Background delivery
# ---------------------------------------------------------------------------


class TestBackgroundDelivery:
    async def test_named_session_success(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.BACKGROUND,
            session_name="research",
            result_text="Found results",
            status="success",
            elapsed_seconds=12.5,
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        mock_send.assert_awaited_once()
        text = mock_send.call_args[0][2]
        assert "[research] Complete" in text
        assert "12s" in text

    async def test_named_session_error(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.BACKGROUND,
            session_name="debug",
            result_text="CLI crash",
            status="error:timeout",
            is_error=True,
            elapsed_seconds=60.0,
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        text = mock_send.call_args[0][2]
        assert "[debug] Failed" in text
        assert "CLI crash" in text

    async def test_named_session_aborted(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.BACKGROUND,
            session_name="task1",
            prompt_preview="do stuff",
            status="aborted",
            elapsed_seconds=0.0,
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        text = mock_send.call_args[0][2]
        assert "[task1] Cancelled" in text

    async def test_stateless_success(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.BACKGROUND,
            result_text="Done",
            status="success",
            elapsed_seconds=5.0,
            metadata={"task_id": "abc123"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        text = mock_send.call_args[0][2]
        assert "Background Task Complete" in text
        assert "Done" in text

    async def test_stateless_error(self) -> None:
        transport, _ = _make_transport()
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
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        text = mock_send.call_args[0][2]
        assert "Background Task Failed" in text
        assert "xyz" in text

    async def test_stateless_aborted(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.BACKGROUND,
            status="aborted",
            prompt_preview="fix bug",
            metadata={"task_id": "t1"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        text = mock_send.call_args[0][2]
        assert "Background Task Cancelled" in text
        assert "t1" in text


# ---------------------------------------------------------------------------
# Heartbeat delivery
# ---------------------------------------------------------------------------


class TestHeartbeatDelivery:
    async def test_delivers_to_room(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.HEARTBEAT,
            chat_id="99",
            result_text="Something happened",
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        mock_send.assert_awaited_once()
        assert mock_send.call_args[0][2] == "Something happened"


# ---------------------------------------------------------------------------
# Inter-agent delivery
# ---------------------------------------------------------------------------


class TestInteragentDelivery:
    async def test_error_sends_notification(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.INTERAGENT,
            is_error=True,
            prompt_preview="translate this",
            metadata={"recipient": "sub-agent", "error": "timeout"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        mock_send.assert_awaited_once()
        text = mock_send.call_args[0][2]
        assert "Inter-Agent Request Failed" in text
        assert "sub-agent" in text
        assert "timeout" in text

    async def test_success_with_provider_switch(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.INTERAGENT,
            result_text="Response text",
            metadata={"provider_switch_notice": "Switched to gemini"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
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


class TestTaskResultDelivery:
    async def test_done_with_injection(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.TASK_RESULT,
            status="done",
            result_text="Injected answer",
            needs_injection=True,
            elapsed_seconds=30.0,
            provider="claude",
            model="opus",
            metadata={"name": "research", "task_id": "t1"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        assert mock_send.await_count == 2
        note = mock_send.call_args_list[0][0][2]
        assert "research" in note
        assert "completed" in note
        assert "claude/opus" in note

    async def test_failed(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.TASK_RESULT,
            status="failed",
            needs_injection=True,
            metadata={"name": "build", "error": "OOM"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        note = mock_send.call_args_list[0][0][2]
        assert "failed" in note
        assert "OOM" in note

    async def test_cancelled(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.TASK_RESULT,
            status="cancelled",
            metadata={"name": "cleanup"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        note = mock_send.call_args_list[0][0][2]
        assert "cancelled" in note


# ---------------------------------------------------------------------------
# Task question delivery
# ---------------------------------------------------------------------------


class TestTaskQuestionDelivery:
    async def test_delivers_question_and_response(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.TASK_QUESTION,
            prompt="What encoding?",
            result_text="Use UTF-8",
            metadata={"task_id": "q1"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        assert mock_send.await_count == 2
        question = mock_send.call_args_list[0][0][2]
        assert "q1" in question
        assert "What encoding?" in question
        answer = mock_send.call_args_list[1][0][2]
        assert answer == "Use UTF-8"


# ---------------------------------------------------------------------------
# Cron broadcast
# ---------------------------------------------------------------------------


class TestCronBroadcast:
    async def test_broadcasts_with_result_text(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.CRON,
            result_text="All good",
            status="success",
            metadata={"title": "Backup"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver_broadcast(env)

        mock_send.assert_awaited_once()
        text = mock_send.call_args[0][2]
        assert "**TASK: Backup**" in text
        assert "All good" in text

    async def test_broadcasts_status_only_when_no_text(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.CRON,
            result_text="",
            status="failed",
            metadata={"title": "Deploy"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver_broadcast(env)

        mock_send.assert_awaited_once()
        text = mock_send.call_args[0][2]
        assert "**TASK: Deploy**" in text
        assert "_failed_" in text

    async def test_skips_ack_only_success(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.CRON,
            result_text='Message sent successfully. "Hi" delivered to Telegram (id 5).',
            status="success",
            metadata={"title": "Greet"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver_broadcast(env)

        mock_send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Webhook cron broadcast
# ---------------------------------------------------------------------------


class TestWebhookCronBroadcast:
    async def test_broadcasts_with_text(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.WEBHOOK_CRON,
            result_text="Deployed v3",
            status="success",
            metadata={"hook_title": "Deploy"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver_broadcast(env)

        mock_send.assert_awaited_once()
        text = mock_send.call_args[0][2]
        assert "WEBHOOK (CRON TASK)" in text
        assert "Deploy" in text
        assert "Deployed v3" in text

    async def test_broadcasts_status_when_no_text(self) -> None:
        transport, _ = _make_transport()
        env = _env(
            origin=Origin.WEBHOOK_CRON,
            result_text="",
            status="error",
            metadata={"hook_title": "Hook"},
        )

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver_broadcast(env)

        mock_send.assert_awaited_once()
        text = mock_send.call_args[0][2]
        assert "_error_" in text


# ---------------------------------------------------------------------------
# Dispatch fallback
# ---------------------------------------------------------------------------


class TestDispatchFallback:
    async def test_unknown_origin_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        transport, _ = _make_transport()
        env = _env(origin=Origin.USER)  # USER has no delivery handler

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ):
            await transport.deliver(env)

        assert "No handler for origin=user" in caplog.text

    async def test_no_room_skips_delivery(self) -> None:
        transport, bot = _make_transport()
        env = _env(origin=Origin.BACKGROUND, result_text="test", status="success", chat_id="")

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        mock_send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Broadcast rooms helper
# ---------------------------------------------------------------------------


class TestBroadcastRooms:
    async def test_broadcast_uses_allowed_rooms(self) -> None:
        transport, bot = _make_transport()
        bot.config.matrix.allowed_rooms = ["!r1:test", "!r2:test"]

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport._broadcast("hello")

        assert mock_send.await_count == 2

    async def test_broadcast_fallback_to_last_active(self) -> None:
        transport, bot = _make_transport()
        bot.config.matrix.allowed_rooms = []
        bot._last_active_room = "!fallback:test"

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport._broadcast("hello")

        mock_send.assert_awaited_once()
        assert mock_send.call_args[0][1] == "!fallback:test"

    async def test_broadcast_no_rooms_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        transport, bot = _make_transport()
        bot.config.matrix.allowed_rooms = []
        bot._last_active_room = None

        with patch(
            "ductor_bot.messenger.matrix.transport.matrix_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport._broadcast("lost message")

        mock_send.assert_not_awaited()
        assert "no rooms available" in caplog.text
