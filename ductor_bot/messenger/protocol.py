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
    from ductor_bot.tasks.models import TaskProgress, TaskResult
    from ductor_bot.workspace.paths import DuctorPaths


@runtime_checkable
class BotProtocol(Protocol):
    """每个传输 bot 都应实现的接口.

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

    async def on_task_progress(self, progress: TaskProgress) -> None:
        """处理权威后台任务生命周期更新."""
        ...

    async def on_task_result(self, result: TaskResult) -> None:
        """Handle background task completion."""
        ...

    async def on_task_question(
        self,
        task_id: str,
        question: str,
        prompt_preview: str,
        chat_id: int,
        thread_id: int | None = None,
    ) -> None:
        """Handle background task question delivery."""
        ...

    def file_roots(self, paths: DuctorPaths) -> list[Path] | None:
        """Allowed root directories for file sends."""
        ...
