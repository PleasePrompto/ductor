"""MultiBotAdapter: wraps multiple transport bots behind a single BotProtocol."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.messenger.notifications import CompositeNotificationService

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig
    from ductor_bot.messenger.notifications import NotificationService
    from ductor_bot.messenger.protocol import BotProtocol
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


class MultiBotAdapter:
    """Wraps multiple transport bots into a single BotProtocol facade.

    The **primary** bot (first transport) creates the orchestrator during
    startup.  Secondary bots receive the orchestrator before their ``run()``
    is called, so their startup skips orchestrator creation.

    All bots share a single ``MessageBus`` and ``LockPool``.
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        agent_name: str = "main",
    ) -> None:
        from ductor_bot.messenger.registry import _create_single_bot

        self._config = config
        self._lock_pool = LockPool()
        self._bus = MessageBus(lock_pool=self._lock_pool)

        transports = config.transports
        if not transports:
            msg = "MultiBotAdapter requires at least one transport"
            raise ValueError(msg)

        bots: list[BotProtocol] = []
        for transport_name in transports:
            bot = _create_single_bot(
                transport_name,
                config,
                agent_name=agent_name,
                bus=self._bus,
                lock_pool=self._lock_pool,
            )
            bots.append(bot)

        self._primary: BotProtocol = bots[0]
        self._secondaries: list[BotProtocol] = bots[1:]
        self._all: list[BotProtocol] = bots

        self._notification_service = CompositeNotificationService()
        for bot in self._all:
            self._notification_service.add(bot.notification_service)

    # -- BotProtocol: properties delegated to primary --------------------------

    @property
    def orchestrator(self) -> Orchestrator | None:
        return self._primary.orchestrator

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def notification_service(self) -> NotificationService:
        return self._notification_service

    # -- BotProtocol: methods delegated to primary -----------------------------

    def register_startup_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        self._primary.register_startup_hook(hook)

    def set_abort_all_callback(self, callback: Callable[[], Awaitable[int]]) -> None:
        for bot in self._all:
            bot.set_abort_all_callback(callback)

    # -- BotProtocol: methods that fan out to all bots -------------------------

    async def on_async_interagent_result(self, result: AsyncInterAgentResult) -> None:
        for bot in self._all:
            await bot.on_async_interagent_result(result)

    def file_roots(self, paths: DuctorPaths) -> list[Path] | None:
        return self._primary.file_roots(paths)

    # -- BotProtocol: run / shutdown -------------------------------------------

    async def run(self) -> int:
        """Start all bots: primary first, then secondaries after orchestrator is ready.

        Returns exit code from the first bot that finishes (e.g. 42 for restart).
        """
        orch_ready = asyncio.Event()

        async def _signal_ready() -> None:
            orch_ready.set()

        self._primary.register_startup_hook(_signal_ready)

        primary_task = asyncio.create_task(self._primary.run(), name="multi:primary")

        await orch_ready.wait()

        # Inject the primary's orchestrator into secondary bots.
        for bot in self._secondaries:
            bot._orchestrator = self._primary.orchestrator  # type: ignore[attr-defined]

        secondary_tasks = [
            asyncio.create_task(bot.run(), name=f"multi:secondary:{i}")
            for i, bot in enumerate(self._secondaries)
        ]

        all_tasks = [primary_task, *secondary_tasks]
        done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        for task in done:
            return task.result()
        return 0

    async def shutdown(self) -> None:
        """Shut down all bots."""
        for bot in self._all:
            await bot.shutdown()
