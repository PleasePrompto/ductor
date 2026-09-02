"""Transport-agnostic bot protocol for the supervisor/stack layer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig
    from ductor_bot.messenger.notifications import NotificationService
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.workspace.paths import DuctorPaths


@runtime_checkable
class BotProtocol(Protocol):
    """Interface that both TelegramBot and MatrixBot implement.

    The supervisor, AgentStack, and InterAgentBus depend ONLY on this protocol,
    never on transport-specific classes.
    """

    @property
    def orchestrator(self) -> Orchestrator | None: ...

    @property
    def config(self) -> AgentConfig: ...

    @property
    def notification_service(self) -> NotificationService: ...

    async def run(self) -> int:
        """Start the bot event loop. Blocks until shutdown. Returns exit code."""
        ...

    async def shutdown(self) -> None:
        """Gracefully shut down the bot."""
        ...

    def register_startup_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Register a callback to run after orchestrator creation."""
        ...

    def set_abort_all_callback(self, callback: Callable[[], Awaitable[int]]) -> None:
        """Set multi-agent abort callback (injected by supervisor)."""
        ...

    async def on_async_interagent_result(self, result: AsyncInterAgentResult) -> None:
        """Handle async inter-agent result delivery."""
        ...

    def file_roots(self, paths: DuctorPaths) -> list[Path] | None:
        """Allowed root directories for file sends."""
        ...
