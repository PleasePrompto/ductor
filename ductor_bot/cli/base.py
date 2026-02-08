"""Base types and abstract interface for CLI backends."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.stream_events import StreamEvent
from ductor_bot.cli.types import CLIResponse

if TYPE_CHECKING:
    from ductor_bot.cli.process_registry import ProcessRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CLIConfig:
    """Configuration for any CLI wrapper."""

    provider: str = "claude"
    working_dir: str | Path = "."
    model: str | None = None
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    permission_mode: str = "bypassPermissions"
    docker_container: str = ""
    # Codex-specific fields (ignored by Claude provider):
    sandbox_mode: str = "read-only"
    images: list[str] = field(default_factory=list)
    instructions: str | None = None
    reasoning_effort: str = "medium"
    # Process tracking (shared across providers):
    process_registry: ProcessRegistry | None = None
    chat_id: int = 0
    process_label: str = "main"


def docker_wrap(
    cmd: list[str],
    docker_container: str,
    chat_id: int,
    working_dir: Path,
) -> tuple[list[str], str | None]:
    """Wrap a CLI command for Docker execution if a container is set."""
    if docker_container:
        logger.debug("docker_wrap container=%s", docker_container)
        return (
            [
                "docker",
                "exec",
                "-e",
                f"DUCTOR_CHAT_ID={chat_id}",
                docker_container,
                *cmd,
            ],
            None,
        )
    return cmd, str(working_dir)


class BaseCLI(ABC):
    """Abstract interface for CLI backends (Claude, Codex, etc.)."""

    @abstractmethod
    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
    ) -> CLIResponse: ...

    @abstractmethod
    def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
    ) -> AsyncGenerator[StreamEvent, None]: ...
