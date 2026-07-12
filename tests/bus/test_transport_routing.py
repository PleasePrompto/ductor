"""Tests for transport-aware delivery routing in the MessageBus.

Verifies that UNICAST envelopes are filtered by transport name,
BROADCAST envelopes go to all transports, and cascading fallback
works when the target transport is unavailable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import DeliveryMode, Envelope, Origin

if TYPE_CHECKING:
    import pytest


def _env(**kwargs: object) -> Envelope:
    """Shortcut for creating test envelopes."""
    defaults: dict[str, object] = {"origin": Origin.CRON, "chat_id": 1}
    defaults.update(kwargs)
    return Envelope(**defaults)  # type: ignore[arg-type]


def _mock_transport(name: str) -> AsyncMock:
    """Create a mock transport with a transport_name attribute."""
    transport = AsyncMock()
    transport.deliver = AsyncMock()
    transport.deliver_broadcast = AsyncMock()
    transport.transport_name = name
    return transport


# -- Transport filtering (UNICAST) --


class TestTransportFiltering:
    async def test_deliver_routes_to_matching_transport(self) -> None:
        """Envelope with transport='tg' -> only TelegramTransport.deliver() called."""
        bus = MessageBus()
        tg = _mock_transport("tg")
        mx = _mock_transport("mx")
        bus.register_transport(tg)
        bus.register_transport(mx)

        env = _env(transport="tg", delivery=DeliveryMode.UNICAST)
        await bus.submit(env)

        tg.deliver.assert_awaited_once_with(env)
        mx.deliver.assert_not_awaited()
        mx.deliver_broadcast.assert_not_awaited()

    async def test_deliver_skips_non_matching_transport(self) -> None:
        """Envelope with transport='tg' -> MatrixTransport.deliver() NOT called."""
        bus = MessageBus()
        tg = _mock_transport("tg")
        mx = _mock_transport("mx")
        bus.register_transport(tg)
        bus.register_transport(mx)

        env = _env(transport="mx", delivery=DeliveryMode.UNICAST)
        await bus.submit(env)

        mx.deliver.assert_awaited_once_with(env)
        tg.deliver.assert_not_awaited()
        tg.deliver_broadcast.assert_not_awaited()

    async def test_deliver_broadcast_goes_to_all(self) -> None:
        """BROADCAST envelopes still go to ALL transports (no filtering)."""
        bus = MessageBus()
        tg = _mock_transport("tg")
        mx = _mock_transport("mx")
        bus.register_transport(tg)
        bus.register_transport(mx)

        env = _env(delivery=DeliveryMode.BROADCAST, transport="tg")
        await bus.submit(env)

        tg.deliver_broadcast.assert_awaited_once_with(env)
        mx.deliver_broadcast.assert_awaited_once_with(env)

    async def test_deliver_without_transport_goes_to_all(self) -> None:
        """Envelope with transport='' -> goes to all (backward compat)."""
        bus = MessageBus()
        tg = _mock_transport("tg")
        mx = _mock_transport("mx")
        bus.register_transport(tg)
        bus.register_transport(mx)

        env = _env(transport="", delivery=DeliveryMode.UNICAST)
        await bus.submit(env)

        tg.deliver.assert_awaited_once_with(env)
        mx.deliver.assert_awaited_once_with(env)


# -- Cascading fallback --


class TestCascadingFallback:
    async def test_deliver_fallback_when_transport_missing(self) -> None:
        """Envelope with transport='mx' but only Telegram registered.

        Telegram receives fallback broadcast with explanation.
        """
        bus = MessageBus()
        tg = _mock_transport("tg")
        bus.register_transport(tg)

        env = _env(
            transport="mx",
            delivery=DeliveryMode.UNICAST,
            result_text="heartbeat alert",
        )
        await bus.submit(env)

        tg.deliver.assert_not_awaited()
        tg.deliver_broadcast.assert_awaited_once()
        fallback_env = tg.deliver_broadcast.call_args[0][0]
        assert "mx" in fallback_env.result_text
        assert "not available" in fallback_env.result_text
        assert "heartbeat alert" in fallback_env.result_text
        assert fallback_env.delivery == DeliveryMode.BROADCAST

    async def test_fallback_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Fallback delivery logs a warning about unavailable transport."""
        bus = MessageBus()
        tg = _mock_transport("tg")
        bus.register_transport(tg)

        env = _env(transport="mx", delivery=DeliveryMode.UNICAST)
        with caplog.at_level("WARNING", logger="ductor_bot.bus.bus"):
            await bus.submit(env)

        assert "mx" in caplog.text
        assert "not available" in caplog.text

    async def test_fallback_picks_first_available_transport(self) -> None:
        """When target transport missing, fallback goes to first other."""
        bus = MessageBus()
        tg = _mock_transport("tg")
        api = _mock_transport("api")
        bus.register_transport(tg)
        bus.register_transport(api)

        env = _env(transport="mx", delivery=DeliveryMode.UNICAST)
        await bus.submit(env)

        tg.deliver_broadcast.assert_awaited_once()
        api.deliver_broadcast.assert_not_awaited()

    async def test_no_fallback_when_transport_matches(self) -> None:
        """No fallback when the target transport is present."""
        bus = MessageBus()
        tg = _mock_transport("tg")
        mx = _mock_transport("mx")
        bus.register_transport(tg)
        bus.register_transport(mx)

        env = _env(transport="tg", delivery=DeliveryMode.UNICAST)
        await bus.submit(env)

        tg.deliver.assert_awaited_once_with(env)
        mx.deliver.assert_not_awaited()
        tg.deliver_broadcast.assert_not_awaited()
        mx.deliver_broadcast.assert_not_awaited()

    async def test_delivery_error_does_not_trigger_fallback(self) -> None:
        """Transport delivery failure is NOT a missing-transport fallback.

        If the matching transport exists but raises, the error is logged
        but no fallback to other transports occurs.
        """
        bus = MessageBus()
        tg = _mock_transport("tg")
        tg.deliver = AsyncMock(side_effect=RuntimeError("Network error"))
        mx = _mock_transport("mx")
        bus.register_transport(tg)
        bus.register_transport(mx)

        env = _env(transport="tg", delivery=DeliveryMode.UNICAST)
        await bus.submit(env)

        tg.deliver.assert_awaited_once()
        mx.deliver.assert_not_awaited()
        mx.deliver_broadcast.assert_not_awaited()

    async def test_fallback_delivery_failure_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When fallback delivery also fails, it's logged but doesn't crash."""
        bus = MessageBus()
        tg = _mock_transport("tg")
        tg.deliver_broadcast = AsyncMock(side_effect=RuntimeError("Fallback crash"))
        bus.register_transport(tg)

        env = _env(transport="mx", delivery=DeliveryMode.UNICAST)
        with caplog.at_level("ERROR", logger="ductor_bot.bus.bus"):
            await bus.submit(env)

        assert "Fallback delivery also failed" in caplog.text


# -- Transport name on real transport classes --


class TestTransportName:
    async def test_telegram_transport_name(self) -> None:
        """TelegramTransport.transport_name returns 'tg'."""
        from unittest.mock import MagicMock

        from ductor_bot.messenger.telegram.transport import TelegramTransport

        bot = MagicMock()
        t = TelegramTransport(bot)
        assert t.transport_name == "tg"

    async def test_matrix_transport_name(self) -> None:
        """MatrixTransport.transport_name returns 'mx'."""
        from unittest.mock import MagicMock

        from ductor_bot.messenger.matrix.transport import MatrixTransport

        bot = MagicMock()
        t = MatrixTransport(bot)
        assert t.transport_name == "mx"

    async def test_slack_transport_name(self) -> None:
        """SlackTransport.transport_name returns 'sl'."""
        from unittest.mock import MagicMock

        from ductor_bot.messenger.slack.transport import SlackTransport

        bot = MagicMock()
        t = SlackTransport(bot)
        assert t.transport_name == "sl"


# -- Adapter transport parameter --


class TestAdapterTransportParam:
    def test_from_cron_result_includes_transport(self) -> None:
        """from_cron_result passes transport to Envelope."""
        from ductor_bot.bus.adapters import from_cron_result

        env = from_cron_result("Title", "Result", "success", chat_id=123, transport="mx")
        assert env.transport == "mx"

    def test_from_cron_result_default_transport(self) -> None:
        """from_cron_result defaults to 'tg' transport."""
        from ductor_bot.bus.adapters import from_cron_result

        env = from_cron_result("Title", "Result", "success", chat_id=123)
        assert env.transport == "tg"

    def test_from_cron_result_broadcast_includes_transport(self) -> None:
        """Broadcast cron (chat_id=0) also accepts transport."""
        from ductor_bot.bus.adapters import from_cron_result

        env = from_cron_result("Title", "Result", "success", transport="mx")
        assert env.transport == "mx"
        assert env.delivery == DeliveryMode.BROADCAST

    def test_from_heartbeat_includes_transport(self) -> None:
        """from_heartbeat passes transport to Envelope."""
        from ductor_bot.bus.adapters import from_heartbeat

        env = from_heartbeat(200, "alert text", transport="mx")
        assert env.transport == "mx"

    def test_from_heartbeat_default_transport(self) -> None:
        """from_heartbeat defaults to 'tg' transport."""
        from ductor_bot.bus.adapters import from_heartbeat

        env = from_heartbeat(200, "alert text")
        assert env.transport == "tg"


# -- CronJob transport field --


class TestCronJobTransport:
    def test_cron_job_has_transport_field(self) -> None:
        """CronJob has a transport field defaulting to 'tg'."""
        from ductor_bot.cron.manager import CronJob

        job = CronJob(
            id="test",
            title="Test",
            description="desc",
            schedule="0 * * * *",
            task_folder="test",
            agent_instruction="do it",
        )
        assert job.transport == "tg"

    def test_cron_job_transport_roundtrip(self) -> None:
        """CronJob transport survives to_dict/from_dict roundtrip."""
        from ductor_bot.cron.manager import CronJob

        job = CronJob(
            id="test",
            title="Test",
            description="desc",
            schedule="0 * * * *",
            task_folder="test",
            agent_instruction="do it",
            transport="mx",
        )
        data = job.to_dict()
        assert data["transport"] == "mx"
        restored = CronJob.from_dict(data)
        assert restored.transport == "mx"

    def test_cron_job_from_dict_default_transport(self) -> None:
        """CronJob.from_dict defaults transport to 'tg' for legacy data."""
        from ductor_bot.cron.manager import CronJob

        data = {
            "id": "old",
            "title": "Old",
            "schedule": "0 * * * *",
            "task_folder": "old",
            "agent_instruction": "do it",
        }
        job = CronJob.from_dict(data)
        assert job.transport == "tg"


# -- Executor DUCTOR_TRANSPORT env var --


class TestExecutorTransportEnv:
    def testbuild_subprocess_env_includes_transport(self) -> None:
        """build_subprocess_env sets DUCTOR_TRANSPORT from CLIConfig."""
        from ductor_bot.cli.base import CLIConfig
        from ductor_bot.cli.executor import build_subprocess_env

        config = CLIConfig(
            working_dir="/tmp",
            chat_id=123,
            transport="mx",
        )
        env = build_subprocess_env(config)
        assert env is not None
        assert env["DUCTOR_TRANSPORT"] == "mx"

    def testbuild_subprocess_env_default_transport(self) -> None:
        """build_subprocess_env defaults DUCTOR_TRANSPORT to 'tg'."""
        from ductor_bot.cli.base import CLIConfig
        from ductor_bot.cli.executor import build_subprocess_env

        config = CLIConfig(working_dir="/tmp", chat_id=123)
        env = build_subprocess_env(config)
        assert env is not None
        assert env["DUCTOR_TRANSPORT"] == "tg"
