"""Tests for SlackTransport delivery handlers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.messenger.slack.transport import SlackTransport


def _make_transport() -> tuple[SlackTransport, MagicMock]:
    bot = MagicMock()
    bot.client = MagicMock()
    bot.orchestrator = MagicMock()
    bot.orchestrator.paths = MagicMock()
    bot.file_roots.return_value = [Path("/tmp/roots")]
    bot.broadcast = AsyncMock()
    bot.id_map.int_to_channel.return_value = "C123"
    bot.id_map.int_to_thread.return_value = ("C123", "1710000000.123")
    transport = SlackTransport(bot)
    return transport, bot


def _env(**kwargs: object) -> Envelope:
    defaults: dict[str, object] = {"origin": Origin.CRON, "chat_id": 42}
    defaults.update(kwargs)
    return Envelope(**defaults)  # type: ignore[arg-type]


class TestTransportName:
    def test_slack_transport_name(self) -> None:
        transport, _bot = _make_transport()
        assert transport.transport_name == "sl"


class TestCronBroadcast:
    async def test_broadcasts_with_result_text(self) -> None:
        transport, bot = _make_transport()
        env = _env(origin=Origin.CRON, result_text="All good", status="success", metadata={"title": "Backup"})

        await transport.deliver_broadcast(env)

        bot.broadcast.assert_awaited_once()
        text = bot.broadcast.call_args[0][0]
        assert "**TASK: Backup**" in text
        assert "All good" in text


class TestTaskQuestionDelivery:
    async def test_delivers_task_question_into_thread(self) -> None:
        transport, _bot = _make_transport()
        env = _env(origin=Origin.TASK_QUESTION, prompt="Need approval", metadata={"task_id": "t1"}, topic_id=9)

        with patch("ductor_bot.messenger.slack.transport.slack_send_rich", new_callable=AsyncMock) as mock_send:
            await transport.deliver(env)

        mock_send.assert_awaited_once()
        assert mock_send.call_args.args[1] == "C123"
        assert "Task `t1` has a question" in mock_send.call_args.args[2]


class TestBackgroundDelivery:
    async def test_delivers_background_result_into_thread(self) -> None:
        transport, _bot = _make_transport()
        env = _env(
            origin=Origin.BACKGROUND,
            result_text="Done",
            status="success",
            elapsed_seconds=2.0,
            topic_id=9,
            metadata={"task_id": "bg1"},
        )

        with patch(
            "ductor_bot.messenger.slack.transport.slack_send_rich", new_callable=AsyncMock
        ) as mock_send:
            await transport.deliver(env)

        mock_send.assert_awaited_once()
        opts = mock_send.call_args.args[3]
        assert opts.thread_ts == "1710000000.123"
