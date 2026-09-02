"""Tests for MultiBotAdapter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.messenger.multi import MultiBotAdapter
from ductor_bot.messenger.notifications import CompositeNotificationService


def _make_config(transports: list[str] | None = None) -> MagicMock:
    """Build a minimal AgentConfig mock."""
    cfg = MagicMock()
    cfg.transports = transports or ["telegram", "matrix"]
    cfg.transport = cfg.transports[0]
    cfg.is_multi_transport = len(cfg.transports) > 1
    return cfg


def _make_bot(*, name: str = "bot") -> MagicMock:
    """Return a mock implementing BotProtocol basics."""
    bot = MagicMock()
    bot.orchestrator = None
    bot.notification_service = MagicMock()
    bot.run = AsyncMock(return_value=0)
    bot.shutdown = AsyncMock()
    bot.register_startup_hook = MagicMock()
    bot.set_abort_all_callback = MagicMock()
    bot.on_async_interagent_result = AsyncMock()
    bot.on_task_result = AsyncMock()
    bot.on_task_question = AsyncMock()
    bot.file_roots = MagicMock(return_value=[Path("/tmp")])
    bot.config = MagicMock()
    bot._name = name
    return bot


class TestMultiBotAdapterCreation:
    def test_creates_correct_number_of_bots(self) -> None:
        config = _make_config(["telegram", "matrix"])
        fake_tg = _make_bot(name="telegram")
        fake_mx = _make_bot(name="matrix")

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            adapter = MultiBotAdapter(config, agent_name="main")

        assert len(adapter._all) == 2
        assert adapter._primary is fake_tg
        assert adapter._secondaries == [fake_mx]

    def test_single_transport_raises_no_error(self) -> None:
        config = _make_config(["telegram"])
        fake_tg = _make_bot(name="telegram")

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            return_value=fake_tg,
        ):
            adapter = MultiBotAdapter(config, agent_name="main")

        assert len(adapter._all) == 1
        assert adapter._secondaries == []

    def test_empty_transports_raises(self) -> None:
        config = _make_config([])
        config.transports = []
        with pytest.raises(ValueError, match="at least one transport"):
            MultiBotAdapter(config)


class TestMultiBotAdapterProperties:
    def test_orchestrator_delegates_to_primary(self) -> None:
        config = _make_config()
        fake_tg = _make_bot()
        fake_mx = _make_bot()
        fake_tg.orchestrator = MagicMock()

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            adapter = MultiBotAdapter(config)

        assert adapter.orchestrator is fake_tg.orchestrator

    def test_config_returns_original_config(self) -> None:
        config = _make_config()
        fake_tg = _make_bot()
        fake_mx = _make_bot()

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            adapter = MultiBotAdapter(config)

        assert adapter.config is config

    def test_notification_service_is_composite(self) -> None:
        config = _make_config()
        fake_tg = _make_bot()
        fake_mx = _make_bot()

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            adapter = MultiBotAdapter(config)

        svc = adapter.notification_service
        assert isinstance(svc, CompositeNotificationService)
        assert len(svc._services) == 2

    def test_file_roots_delegates_to_primary(self) -> None:
        config = _make_config()
        fake_tg = _make_bot()
        fake_mx = _make_bot()
        paths = MagicMock()

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            adapter = MultiBotAdapter(config)

        result = adapter.file_roots(paths)
        fake_tg.file_roots.assert_called_once_with(paths)
        assert result == fake_tg.file_roots(paths)


class TestMultiBotAdapterDelegation:
    def test_register_startup_hook_delegates_to_primary(self) -> None:
        config = _make_config()
        fake_tg = _make_bot()
        fake_mx = _make_bot()

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            adapter = MultiBotAdapter(config)

        hook = AsyncMock()
        adapter.register_startup_hook(hook)
        fake_tg.register_startup_hook.assert_called_once_with(hook)
        fake_mx.register_startup_hook.assert_not_called()

    def test_set_abort_all_callback_fans_out(self) -> None:
        config = _make_config()
        fake_tg = _make_bot()
        fake_mx = _make_bot()

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            adapter = MultiBotAdapter(config)

        cb = AsyncMock()
        adapter.set_abort_all_callback(cb)
        fake_tg.set_abort_all_callback.assert_called_once_with(cb)
        fake_mx.set_abort_all_callback.assert_called_once_with(cb)

    async def test_on_async_interagent_result_fans_out(self) -> None:
        config = _make_config()
        fake_tg = _make_bot()
        fake_mx = _make_bot()

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            adapter = MultiBotAdapter(config)

        result = MagicMock()
        await adapter.on_async_interagent_result(result)
        fake_tg.on_async_interagent_result.assert_awaited_once_with(result)
        fake_mx.on_async_interagent_result.assert_awaited_once_with(result)

    async def test_run_starts_primary_then_secondaries(self) -> None:
        """Primary starts first; secondaries start after orch_ready."""
        config = _make_config()
        fake_tg = _make_bot()
        fake_mx = _make_bot()
        fake_orch = MagicMock()
        fake_tg.orchestrator = fake_orch

        start_order: list[str] = []

        # Capture the startup hook registered by MultiBotAdapter
        registered_hooks: list[Callable[[], Awaitable[None]]] = []

        def capture_hook(hook: Callable[[], Awaitable[None]]) -> None:
            registered_hooks.append(hook)

        fake_tg.register_startup_hook = MagicMock(side_effect=capture_hook)

        async def primary_run() -> int:
            start_order.append("primary")
            # Simulate startup: call the registered hook
            for hook in registered_hooks:
                await hook()
            # Wait a bit so secondary can start
            await asyncio.sleep(0.05)
            return 0

        async def secondary_run() -> int:
            start_order.append("secondary")
            return 0

        fake_tg.run = primary_run
        fake_mx.run = secondary_run

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            adapter = MultiBotAdapter(config)

        code = await adapter.run()
        assert code == 0
        assert start_order[0] == "primary"
        assert "secondary" in start_order

    async def test_run_injects_orchestrator_into_secondaries(self) -> None:
        config = _make_config()
        fake_tg = _make_bot()
        fake_mx = _make_bot()
        fake_orch = MagicMock()
        fake_tg.orchestrator = fake_orch

        registered_hooks: list[Callable[[], Awaitable[None]]] = []

        def capture_hook(hook: Callable[[], Awaitable[None]]) -> None:
            registered_hooks.append(hook)

        fake_tg.register_startup_hook = MagicMock(side_effect=capture_hook)

        captured_orch = None

        async def primary_run() -> int:
            for hook in registered_hooks:
                await hook()
            await asyncio.sleep(0.05)
            return 0

        async def secondary_run() -> int:
            nonlocal captured_orch
            captured_orch = fake_mx._orchestrator
            return 0

        fake_tg.run = primary_run
        fake_mx.run = secondary_run

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            adapter = MultiBotAdapter(config)

        await adapter.run()
        assert captured_orch is fake_orch

    async def test_run_returns_exit_restart_on_first_finish(self) -> None:
        config = _make_config()
        fake_tg = _make_bot()
        fake_mx = _make_bot()
        fake_tg.orchestrator = MagicMock()

        registered_hooks: list[Callable[[], Awaitable[None]]] = []

        def capture_hook(hook: Callable[[], Awaitable[None]]) -> None:
            registered_hooks.append(hook)

        fake_tg.register_startup_hook = MagicMock(side_effect=capture_hook)

        async def primary_run() -> int:
            for hook in registered_hooks:
                await hook()
            # Run for a while
            await asyncio.sleep(10)
            return 0

        async def secondary_returns_restart() -> int:
            return 42

        fake_tg.run = primary_run
        fake_mx.run = secondary_returns_restart

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            adapter = MultiBotAdapter(config)

        code = await adapter.run()
        assert code == 42

    async def test_shutdown_all_bots(self) -> None:
        config = _make_config()
        fake_tg = _make_bot()
        fake_mx = _make_bot()

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            adapter = MultiBotAdapter(config)

        await adapter.shutdown()
        fake_tg.shutdown.assert_awaited_once()
        fake_mx.shutdown.assert_awaited_once()


class TestCompositeNotificationService:
    async def test_notify_fans_out(self) -> None:
        svc = CompositeNotificationService()
        s1 = AsyncMock()
        s2 = AsyncMock()
        svc.add(s1)
        svc.add(s2)

        await svc.notify(123, "hello")
        s1.notify.assert_awaited_once_with(123, "hello")
        s2.notify.assert_awaited_once_with(123, "hello")

    async def test_notify_all_fans_out(self) -> None:
        svc = CompositeNotificationService()
        s1 = AsyncMock()
        s2 = AsyncMock()
        svc.add(s1)
        svc.add(s2)

        await svc.notify_all("broadcast")
        s1.notify_all.assert_awaited_once_with("broadcast")
        s2.notify_all.assert_awaited_once_with("broadcast")


class TestRegistryMultiTransport:
    def test_single_transport_returns_single_bot(self) -> None:
        """When is_multi_transport is False, create_bot returns a plain bot."""
        from ductor_bot.messenger.registry import create_bot

        config = MagicMock()
        config.transport = "telegram"
        config.is_multi_transport = False
        fake_bot = MagicMock()

        with patch(
            "ductor_bot.messenger.telegram.app.TelegramBot",
            return_value=fake_bot,
        ):
            bot = create_bot(config, agent_name="test")

        assert bot is fake_bot
        assert not isinstance(bot, MultiBotAdapter)

    def test_multi_transport_returns_adapter(self) -> None:
        """When is_multi_transport is True, create_bot returns MultiBotAdapter."""
        from ductor_bot.messenger.registry import create_bot

        config = _make_config(["telegram", "matrix"])
        fake_tg = _make_bot()
        fake_mx = _make_bot()

        with patch(
            "ductor_bot.messenger.registry._create_single_bot",
            side_effect=[fake_tg, fake_mx],
        ):
            bot = create_bot(config, agent_name="test")

        assert isinstance(bot, MultiBotAdapter)
