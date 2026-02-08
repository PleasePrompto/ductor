"""Command registry and OrchestratorResult."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator

CommandHandler = Callable[["Orchestrator", int, str], Awaitable["OrchestratorResult | None"]]


class OrchestratorResult(BaseModel):
    """Structured return from handle_message."""

    model_config = {"arbitrary_types_allowed": True}

    text: str
    stream_fallback: bool = False
    reply_markup: Any = Field(default=None, exclude=True)


@dataclass(frozen=True, slots=True)
class _CommandEntry:
    name: str
    handler: CommandHandler
    match_prefix: bool


class CommandRegistry:
    """Registry of slash commands with async dispatch."""

    def __init__(self) -> None:
        self._commands: list[_CommandEntry] = []

    def register_async(self, name: str, handler: CommandHandler) -> None:
        self._commands.append(
            _CommandEntry(name=name, handler=handler, match_prefix=name.endswith(" "))
        )

    async def dispatch(
        self,
        cmd: str,
        orch: Orchestrator,
        chat_id: int,
        text: str,
    ) -> OrchestratorResult | None:
        """Dispatch *cmd* to a registered handler. Returns None if unknown."""
        for entry in self._commands:
            if entry.match_prefix:
                if cmd.startswith(entry.name):
                    logger.debug("Command matched cmd=%s", entry.name)
                    return await entry.handler(orch, chat_id, text)
            elif cmd == entry.name:
                logger.debug("Command matched cmd=%s", entry.name)
                return await entry.handler(orch, chat_id, text)
        return None
