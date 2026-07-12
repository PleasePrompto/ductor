"""Tests for ObserverManager.wire_to_bus()."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import Origin
from ductor_bot.orchestrator.observers import ObserverManager


def _make_observers() -> ObserverManager:
    """Build an ObserverManager with mocked sub-observers."""
    config = MagicMock()
    paths = MagicMock()
    mgr = ObserverManager(config, paths)
    # Replace heartbeat with a mock that accepts set_result_handler
    mgr.heartbeat = MagicMock()
    return mgr


# ---------------------------------------------------------------------------
# wire_to_bus wiring
# ---------------------------------------------------------------------------


class TestWireToBus:
    def test_heartbeat_handler_wired(self) -> None:
        mgr = _make_observers()
        bus = MessageBus()
        mgr.wire_to_bus(bus)
        mgr.heartbeat.set_result_handler.assert_called_once()

    def test_cron_handler_wired_when_present(self) -> None:
        mgr = _make_observers()
        mgr.cron = MagicMock()
        bus = MessageBus()
        mgr.wire_to_bus(bus)
        mgr.cron.set_result_handler.assert_called_once()

    def test_cron_handler_skipped_when_none(self) -> None:
        mgr = _make_observers()
        mgr.cron = None
        bus = MessageBus()
        mgr.wire_to_bus(bus)  # No error

    def test_background_handler_wired_when_present(self) -> None:
        mgr = _make_observers()
        mgr.background = MagicMock()
        bus = MessageBus()
        mgr.wire_to_bus(bus)
        mgr.background.set_result_handler.assert_called_once()

    def test_background_handler_skipped_when_none(self) -> None:
        mgr = _make_observers()
        mgr.background = None
        bus = MessageBus()
        mgr.wire_to_bus(bus)  # No error

    def test_webhook_handler_wired_when_present(self) -> None:
        mgr = _make_observers()
        mgr.webhook = MagicMock()
        bus = MessageBus()
        mgr.wire_to_bus(bus)
        mgr.webhook.set_result_handler.assert_called_once()

    def test_webhook_handler_skipped_when_none(self) -> None:
        mgr = _make_observers()
        mgr.webhook = None
        bus = MessageBus()
        mgr.wire_to_bus(bus)  # No error

    def test_wake_handler_passed_to_webhook(self) -> None:
        mgr = _make_observers()
        mgr.webhook = MagicMock()
        bus = MessageBus()
        wake = AsyncMock()
        mgr.wire_to_bus(bus, wake_handler=wake)
        mgr.webhook.set_wake_handler.assert_called_once_with(wake)

    def test_wake_handler_not_set_when_none(self) -> None:
        mgr = _make_observers()
        mgr.webhook = MagicMock()
        bus = MessageBus()
        mgr.wire_to_bus(bus, wake_handler=None)
        mgr.webhook.set_wake_handler.assert_not_called()


# ---------------------------------------------------------------------------
# Webhook callback filters wake mode
# ---------------------------------------------------------------------------


class TestWebhookWakeFilter:
    async def test_webhook_callback_skips_wake_mode(self) -> None:
        from ductor_bot.webhook.models import WebhookResult

        mgr = _make_observers()
        mgr.webhook = MagicMock()
        bus = MessageBus()
        transport = AsyncMock()
        transport.transport_name = "tg"
        bus.register_transport(transport)
        mgr.wire_to_bus(bus)

        # Capture the handler passed to webhook.set_result_handler
        handler = mgr.webhook.set_result_handler.call_args[0][0]

        result = WebhookResult(
            hook_id="h1",
            hook_title="Test",
            mode="wake",
            result_text="text",
            status="success",
        )
        await handler(result)
        transport.deliver_broadcast.assert_not_awaited()

    async def test_webhook_callback_submits_cron_task(self) -> None:
        from ductor_bot.webhook.models import WebhookResult

        mgr = _make_observers()
        mgr.webhook = MagicMock()
        bus = MessageBus()
        transport = AsyncMock()
        transport.transport_name = "tg"
        bus.register_transport(transport)
        mgr.wire_to_bus(bus)

        handler = mgr.webhook.set_result_handler.call_args[0][0]

        result = WebhookResult(
            hook_id="h1",
            hook_title="Deploy",
            mode="cron_task",
            result_text="Deployed v3",
            status="success",
        )
        await handler(result)
        transport.deliver_broadcast.assert_awaited_once()
        env = transport.deliver_broadcast.call_args[0][0]
        assert env.origin == Origin.WEBHOOK_CRON


# ---------------------------------------------------------------------------
# Integration: wired callback actually reaches bus
# ---------------------------------------------------------------------------


class TestWireIntegration:
    async def test_heartbeat_callback_submits_to_bus(self) -> None:
        mgr = _make_observers()
        bus = MessageBus()
        transport = AsyncMock()
        transport.transport_name = "tg"
        bus.register_transport(transport)
        mgr.wire_to_bus(bus)

        handler = mgr.heartbeat.set_result_handler.call_args[0][0]
        await handler(99, "Alert text")

        transport.deliver.assert_awaited_once()
        env = transport.deliver.call_args[0][0]
        assert env.origin == Origin.HEARTBEAT
        assert env.chat_id == 99
        assert env.topic_id is None
        assert env.result_text == "Alert text"
        assert env.transport == "tg"

    async def test_heartbeat_callback_submits_topic_id_to_bus(self) -> None:
        mgr = _make_observers()
        bus = MessageBus()
        transport = AsyncMock()
        transport.transport_name = "sl"
        bus.register_transport(transport)
        mgr.wire_to_bus(bus)

        handler = mgr.heartbeat.set_result_handler.call_args[0][0]
        await handler(-1001, "Group alert", 42, "sl")

        transport.deliver.assert_awaited_once()
        env = transport.deliver.call_args[0][0]
        assert env.origin == Origin.HEARTBEAT
        assert env.chat_id == -1001
        assert env.topic_id == 42
        assert env.result_text == "Group alert"
        assert env.transport == "sl"

    async def test_cron_callback_submits_to_bus(self) -> None:
        mgr = _make_observers()
        mgr.cron = MagicMock()
        bus = MessageBus()
        transport = AsyncMock()
        transport.transport_name = "sl"
        bus.register_transport(transport)
        mgr.wire_to_bus(bus)

        handler = mgr.cron.set_result_handler.call_args[0][0]
        await handler("Backup", "Done", "success")

        transport.deliver_broadcast.assert_awaited_once()
        env = transport.deliver_broadcast.call_args[0][0]
        assert env.origin == Origin.CRON
        assert env.metadata["title"] == "Backup"

    async def test_background_callback_submits_to_bus(self) -> None:
        mgr = _make_observers()
        mgr.background = MagicMock()
        bus = MessageBus()
        transport = AsyncMock()
        transport.transport_name = "sl"
        bus.register_transport(transport)
        mgr.wire_to_bus(bus)

        handler = mgr.background.set_result_handler.call_args[0][0]

        bg_result = MagicMock()
        bg_result.chat_id = 42
        bg_result.prompt_preview = "test"
        bg_result.result_text = "done"
        bg_result.status = "success"
        bg_result.message_id = 1
        bg_result.thread_id = 77
        bg_result.transport = "sl"
        bg_result.elapsed_seconds = 5.0
        bg_result.provider = "claude"
        bg_result.model = "sonnet"
        bg_result.session_name = ""
        bg_result.session_id = ""
        bg_result.task_id = "t1"
        await handler(bg_result)

        transport.deliver.assert_awaited_once()
        env = transport.deliver.call_args[0][0]
        assert env.origin == Origin.BACKGROUND
        assert env.chat_id == 42
        assert env.topic_id == 77
        assert env.transport == "sl"
