"""Base types and abstract interface for CLI backends."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import sys
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.stream_events import StreamEvent
from ductor_bot.cli.types import CLIResponse

if TYPE_CHECKING:
    from ductor_bot.cli.process_registry import ProcessRegistry
    from ductor_bot.cli.timeout_controller import TimeoutController

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"


def _win_feed_stdin(process: asyncio.subprocess.Process, data: str) -> None:
    """Write prompt to stdin and close on Windows; no-op on POSIX."""
    if _IS_WINDOWS and process.stdin is not None:
        process.stdin.write(data.encode())
        process.stdin.close()


async def _feed_stdin_and_close(
    process: asyncio.subprocess.Process,
    data: str,
    *,
    windows_only: bool = False,
) -> None:
    """Write prompt to stdin and close the writer gracefully."""
    if windows_only and not _IS_WINDOWS:
        return

    writer = getattr(process, "stdin", None)
    if writer is None:
        return

    with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError, ValueError):
        writer.write(data.encode())
        drain_result = writer.drain()
        if inspect.isawaitable(drain_result):
            await drain_result

    writer.close()
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is None:
        return
    with contextlib.suppress(
        BrokenPipeError,
        ConnectionResetError,
        RuntimeError,
        OSError,
        ValueError,
    ):
        closed_result = wait_closed()
        if inspect.isawaitable(closed_result):
            await closed_result


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
    topic_id: int | None = None
    process_label: str = "main"
    # Gemini-specific auth fallback:
    gemini_api_key: str | None = None
    # Extra CLI parameters (provider-specific):
    cli_parameters: list[str] = field(default_factory=list)
    # Transport identification (for routing results back):
    transport: str = "tg"
    # Multi-agent identification:
    agent_name: str = "main"
    interagent_port: int = 8799
    # External transcription hooks (#66) — empty strings keep built-in strategies.
    transcribe_command: str = ""
    video_transcribe_command: str = ""


_CONTAINER_DUCTOR_MOUNT = "/ductor"


def _to_container_path(host_path: Path, main_home: Path) -> str:
    """Map a host path under *main_home* to its container equivalent.

    The Docker container mounts the root ductor home at ``/ductor``.
    """
    rel = host_path.relative_to(main_home)
    if str(rel) == ".":
        return _CONTAINER_DUCTOR_MOUNT
    return f"{_CONTAINER_DUCTOR_MOUNT}/{rel.as_posix()}"


def _docker_env_flags(
    config: CLIConfig,
    container_home: str,
    container_shared: str,
) -> list[str]:
    """Build the ``-e KEY=VAL`` argv flags for ``docker exec``."""
    env_flags: list[str] = [
        "-e",
        f"DUCTOR_CHAT_ID={config.chat_id}",
        "-e",
        f"DUCTOR_TRANSPORT={config.transport}",
        "-e",
        f"DUCTOR_AGENT_NAME={config.agent_name}",
        "-e",
        f"DUCTOR_INTERAGENT_PORT={config.interagent_port}",
        "-e",
        f"DUCTOR_HOME={container_home}",
        "-e",
        f"DUCTOR_SHARED_MEMORY_PATH={container_shared}",
        "-e",
        "DUCTOR_INTERAGENT_HOST=host.docker.internal",
    ]
    if config.topic_id:
        env_flags += ["-e", f"DUCTOR_TOPIC_ID={config.topic_id}"]
    if config.transcribe_command:
        env_flags += ["-e", f"DUCTOR_TRANSCRIBE_COMMAND={config.transcribe_command}"]
    if config.video_transcribe_command:
        env_flags += [
            "-e",
            f"DUCTOR_VIDEO_TRANSCRIBE_COMMAND={config.video_transcribe_command}",
        ]
    return env_flags


def docker_wrap(
    cmd: list[str],
    config: CLIConfig,
    *,
    extra_env: dict[str, str] | None = None,
    interactive: bool = False,
) -> tuple[list[str], str | None]:
    """Wrap a CLI command for Docker execution if a container is set.

    *interactive* adds ``-i`` to keep stdin open (required for providers
    that pipe the prompt via stdin, e.g. Gemini).

    *extra_env* vars are injected as ``-e`` flags into ``docker exec``
    (set **inside** the container, unlike ``env=`` on the host process).
    """
    if config.docker_container:
        logger.debug("docker_wrap container=%s", config.docker_container)
        stdin_flag: list[str] = ["-i"] if interactive else []
        working_dir = Path(config.working_dir)
        ductor_home = working_dir.parent if working_dir.name == "workspace" else working_dir

        # Resolve root ductor home for host → container path mapping.
        # Sub-agents live at <root>/agents/<name>/; the Docker mount is the root.
        main_home = ductor_home
        if main_home.parent.name == "agents":
            main_home = main_home.parent.parent

        container_cwd = _to_container_path(working_dir, main_home)
        container_home = _to_container_path(ductor_home, main_home)
        container_shared = _to_container_path(main_home / "SHAREDMEMORY.md", main_home)

        # Merge user secrets from .env (low priority — never override).
        import os

        from ductor_bot.infra.env_secrets import load_env_secrets

        merged_extra = dict(load_env_secrets(main_home / ".env"))
        # Remove keys already in host env (subprocess inherits docker binary env).
        for key in list(merged_extra):
            if key in os.environ:
                del merged_extra[key]
        if extra_env:
            merged_extra.update(extra_env)  # Provider-specific overrides win.
        extra_env = merged_extra or None

        env_flags = _docker_env_flags(config, container_home, container_shared)
        if extra_env:
            for key, value in extra_env.items():
                env_flags += ["-e", f"{key}={value}"]
        return (
            [
                "docker",
                "exec",
                *stdin_flag,
                "-w",
                container_cwd,
                *env_flags,
                config.docker_container,
                *cmd,
            ],
            None,
        )
    return cmd, str(Path(config.working_dir).resolve())


class BaseCLI(ABC):
    """Abstract interface for CLI backends (Claude, Codex, etc.)."""

    @abstractmethod
    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> CLIResponse: ...

    @abstractmethod
    def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> AsyncGenerator[StreamEvent, None]: ...
