"""Shared subprocess execution for CLI providers.

Centralises the duplicated subprocess lifecycle (creation, stdin feeding,
process-registry tracking, stderr draining, streaming read-loop with timeout,
and cleanup) that was repeated across ``claude_provider`` and ``codex_provider``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

from ductor_bot.cli.base import (
    _IS_WINDOWS,
    CLIConfig,
    _feed_stdin_and_close,
    _win_feed_stdin,
)
from ductor_bot.cli.stream_events import ResultEvent, StreamEvent
from ductor_bot.cli.timeout_controller import TimeoutController
from ductor_bot.cli.types import CLIResponse, task_id_from_label
from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

logger = logging.getLogger(__name__)


def build_subprocess_env(config: CLIConfig) -> dict[str, str] | None:
    """Build environment dict with agent identification vars.

    Returns None if no extra vars are needed (avoids inheriting a stripped env).
    For non-Docker execution, the subprocess inherits the parent env plus the
    agent identification variables.  User secrets from ``~/.ductor/.env`` are
    merged in without overriding existing variables.
    """
    import os
    from pathlib import Path

    from ductor_bot.infra.env_secrets import load_env_secrets

    env = os.environ.copy()

    # Merge user secrets (low priority — never override existing vars).
    working_dir = Path(config.working_dir)
    ductor_home = working_dir.parent if working_dir.name == "workspace" else working_dir
    env_file = ductor_home / ".env"
    for key, value in load_env_secrets(env_file).items():
        if key not in env:
            env[key] = value

    env["DUCTOR_AGENT_NAME"] = config.agent_name
    env["DUCTOR_AGENT_ROLE"] = "main" if config.agent_name == "main" else "sub"
    env["DUCTOR_INTERAGENT_PORT"] = str(config.interagent_port)
    if config.chat_id:
        env["DUCTOR_CHAT_ID"] = str(config.chat_id)
    if config.topic_id:
        env["DUCTOR_TOPIC_ID"] = str(config.topic_id)
    env["DUCTOR_TRANSPORT"] = config.transport
    if task_id := task_id_from_label(config.process_label):
        env["DUCTOR_TASK_ID"] = task_id
    if config.transcribe_command:
        env["DUCTOR_TRANSCRIBE_COMMAND"] = config.transcribe_command
    if config.video_transcribe_command:
        env["DUCTOR_VIDEO_TRANSCRIBE_COMMAND"] = config.video_transcribe_command
    working_dir = Path(config.working_dir)
    ductor_home = working_dir.parent if working_dir.name == "workspace" else working_dir
    env["DUCTOR_HOME"] = str(ductor_home)
    # Shared knowledge is always at the main agent's home level.
    # For main: ductor_home itself. For sub-agents: ../../ from agents/<name>/.
    if config.agent_name == "main":
        env["DUCTOR_SHARED_MEMORY_PATH"] = str(ductor_home / "SHAREDMEMORY.md")
    else:
        # Sub-agent home is <main_home>/agents/<name>/
        main_home = ductor_home.parent.parent
        env["DUCTOR_SHARED_MEMORY_PATH"] = str(main_home / "SHAREDMEMORY.md")
    return env


@dataclass(slots=True)
class SubprocessSpec:
    """What to run: command, working directory, prompt, timeout, and stdin."""

    exec_cmd: list[str]
    use_cwd: str | None
    prompt: str
    timeout_seconds: float | None = None
    timeout_controller: TimeoutController | None = None
    stdin_text: str | None = None


@dataclass(slots=True)
class SubprocessResult:
    """Outcome of a completed streaming subprocess."""

    process: asyncio.subprocess.Process
    stderr_bytes: bytes


# ---------------------------------------------------------------------------
# Streaming subprocess
# ---------------------------------------------------------------------------

LineHandler = Callable[[str], AsyncGenerator[StreamEvent, None]]
"""Async generator that receives a decoded stdout line and yields events."""

PostHandler = Callable[[SubprocessResult], AsyncGenerator[StreamEvent, None]]
"""Async generator that receives the subprocess result after stream ends."""


async def _default_post_handler(result: SubprocessResult) -> AsyncGenerator[StreamEvent, None]:
    """Yield an error ``ResultEvent`` when the process exited non-zero."""
    if result.process.returncode != 0:
        stderr_text = (
            result.stderr_bytes.decode(errors="replace")[:2000] if result.stderr_bytes else ""
        )
        yield ResultEvent(
            type="result",
            result=stderr_text[:500],
            is_error=True,
            returncode=result.process.returncode,
        )


async def run_streaming_subprocess(
    config: CLIConfig,
    spec: SubprocessSpec,
    line_handler: LineHandler,
    *,
    provider_label: str = "CLI",
    post_handler: PostHandler | None = None,
) -> AsyncGenerator[StreamEvent, None]:
    """Spawn a subprocess and stream stdout lines through *line_handler*.

    Lifecycle:
    1. Create subprocess with stdout/stderr pipes
    2. Feed stdin when requested (or on Windows legacy prompt pipe)
    3. Register in process registry
    4. Drain stderr in background task
    5. Stream stdout lines through *line_handler* with timeout
    6. On timeout: kill, yield error, return
    7. Cleanup: cancel drain, unregister tracked process
    8. Post-loop: delegate to *post_handler* (default: yield error on non-zero exit)
    """
    subprocess_env = build_subprocess_env(config) if spec.use_cwd else None
    process = await asyncio.create_subprocess_exec(
        *spec.exec_cmd,
        stdin=_stdin_pipe(spec),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=spec.use_cwd,
        env=subprocess_env,
        limit=4 * 1024 * 1024,
        creationflags=_CREATION_FLAGS,
    )
    if process.stdout is None or process.stderr is None:
        msg = "Subprocess created without stdout/stderr pipes"
        raise RuntimeError(msg)
    # Feed stdin concurrently with the stdout read loop: a prompt larger than
    # the OS pipe buffer (~64 KiB) would otherwise deadlock against a child
    # that starts emitting stdout before draining stdin.
    stdin_feed = asyncio.create_task(_feed_streaming_stdin(process, spec))
    logger.info("%s subprocess starting pid=%s", provider_label, process.pid)

    reg = config.process_registry
    tracked = (
        reg.register(config.chat_id, process, config.process_label, topic_id=config.topic_id)
        if reg
        else None
    )
    stderr_drain = asyncio.create_task(process.stderr.read())

    try:
        async for event in _stream_with_timeout(process, spec, line_handler):
            yield event
        stderr_bytes = await stderr_drain
    except TimeoutError:
        force_kill_process_tree(process.pid)
        await process.wait()
        timeout_s = spec.timeout_seconds or 0
        logger.warning("%s stream timed out after %.0fs", provider_label, timeout_s)
        yield ResultEvent(
            type="result",
            result=f"__TIMEOUT__{int(timeout_s)}",
            is_error=True,
        )
        return
    finally:
        if not stdin_feed.done():
            stdin_feed.cancel()
        with contextlib.suppress(BaseException):
            await stdin_feed
        await _cancel_drain(stderr_drain)
        if tracked and reg:
            reg.unregister(tracked)

    await process.wait()

    handler = post_handler or _default_post_handler
    async for event in handler(SubprocessResult(process=process, stderr_bytes=stderr_bytes)):
        yield event


# ---------------------------------------------------------------------------
# Streaming timeout strategies
# ---------------------------------------------------------------------------


async def _stream_with_timeout(
    process: asyncio.subprocess.Process,
    spec: SubprocessSpec,
    line_handler: LineHandler,
) -> AsyncGenerator[StreamEvent, None]:
    """Read stdout lines with either a plain timeout or a managed controller.

    When ``spec.timeout_controller`` is set, the controller manages deadline
    extensions triggered by output activity and fires warning callbacks.
    Otherwise a plain ``asyncio.timeout`` is used (backward-compatible).
    """
    if spec.timeout_controller:
        async for event in _stream_with_controller(process, spec.timeout_controller, line_handler):
            yield event
    else:
        async with asyncio.timeout(spec.timeout_seconds):
            while True:
                line_bytes = await process.stdout.readline()  # type: ignore[union-attr]
                if not line_bytes:
                    break
                line = line_bytes.decode(errors="replace").rstrip()
                logger.debug("Stream line: %s", line[:120])
                async for event in line_handler(line):
                    yield event


async def _stream_with_controller(
    process: asyncio.subprocess.Process,
    tc: TimeoutController,
    line_handler: LineHandler,
) -> AsyncGenerator[StreamEvent, None]:
    """Streaming read loop managed by a :class:`TimeoutController`.

    Uses ``asyncio.timeout`` with a retry-on-extend pattern:  when the timeout
    fires but the controller grants an extension (recent activity + budget),
    a new timeout context is entered to continue reading.
    """
    tc.begin()
    warning_task = tc.start_warning_loop()

    try:
        timeout_secs = tc.timeout_seconds
        while True:
            try:
                async with asyncio.timeout(timeout_secs):
                    while True:
                        line_bytes = await process.stdout.readline()  # type: ignore[union-attr]
                        if not line_bytes:
                            return  # EOF
                        tc.record_activity()
                        line = line_bytes.decode(errors="replace").rstrip()
                        logger.debug("Stream line: %s", line[:120])
                        async for event in line_handler(line):
                            yield event
            except TimeoutError:
                if tc.try_extend():
                    timeout_secs = tc.activity_extension_seconds
                    continue
                raise
    finally:
        if warning_task and not warning_task.done():
            warning_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await warning_task


# ---------------------------------------------------------------------------
# Non-streaming subprocess
# ---------------------------------------------------------------------------


async def run_oneshot_subprocess(
    config: CLIConfig,
    spec: SubprocessSpec,
    parse_output: Callable[[bytes, bytes, int | None], CLIResponse],
    *,
    provider_label: str = "CLI",
) -> CLIResponse:
    """Run a subprocess, wait for completion, return parsed output.

    Lifecycle:
    1. Create subprocess with pipes
    2. Communicate (optional stdin + wait)
    3. Register/unregister in process registry
    4. Handle timeout
    5. Parse output via *parse_output* callback
    """
    oneshot_env = build_subprocess_env(config) if spec.use_cwd else None
    process = await asyncio.create_subprocess_exec(
        *spec.exec_cmd,
        stdin=_stdin_pipe(spec),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=spec.use_cwd,
        env=oneshot_env,
        creationflags=_CREATION_FLAGS,
    )
    logger.info("%s subprocess starting pid=%s", provider_label, process.pid)

    reg = config.process_registry
    tracked = (
        reg.register(config.chat_id, process, config.process_label, topic_id=config.topic_id)
        if reg
        else None
    )
    try:
        stdin_data = _stdin_bytes(spec)
        if spec.timeout_controller:
            communicate_coro = process.communicate(input=stdin_data)
            stdout, stderr = await spec.timeout_controller.run_with_timeout(communicate_coro)
        else:
            async with asyncio.timeout(spec.timeout_seconds):
                stdout, stderr = await process.communicate(input=stdin_data)
    except TimeoutError:
        force_kill_process_tree(process.pid)
        await process.wait()
        logger.warning("%s timed out after %.0fs", provider_label, spec.timeout_seconds)
        return CLIResponse(result="", is_error=True, timed_out=True)
    finally:
        if tracked and reg:
            reg.unregister(tracked)

    return parse_output(stdout, stderr, process.returncode)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stdin_pipe(spec: SubprocessSpec) -> int | None:
    """Return a stdin pipe when the provider supplies stdin or Windows needs it."""
    return asyncio.subprocess.PIPE if spec.stdin_text is not None or _IS_WINDOWS else None


def _stdin_bytes(spec: SubprocessSpec) -> bytes | None:
    """Return bytes to feed to communicate(), preserving Windows legacy behavior."""
    if spec.stdin_text is not None:
        return spec.stdin_text.encode()
    return spec.prompt.encode() if _IS_WINDOWS else None


async def _feed_streaming_stdin(
    process: asyncio.subprocess.Process,
    spec: SubprocessSpec,
) -> None:
    """Feed stdin for streaming processes, using the old Windows fallback when needed."""
    if spec.stdin_text is not None:
        await _feed_stdin_and_close(process, spec.stdin_text)
        return
    _win_feed_stdin(process, spec.prompt)


async def _cancel_drain(drain: asyncio.Task[bytes]) -> None:
    """Cancel a stderr drain task and silently absorb any resulting exception."""
    if not drain.done():
        drain.cancel()
        with contextlib.suppress(BaseException):
            await drain
