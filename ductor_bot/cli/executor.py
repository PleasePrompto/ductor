"""Shared subprocess execution for CLI providers.

Centralises the duplicated subprocess lifecycle (creation, stdin feeding,
process-registry tracking, stderr draining, streaming read-loop with timeout,
and cleanup) that was repeated across ``claude_provider`` and ``codex_provider``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

from ductor_bot.cli.base import (
    _CREATION_FLAGS,
    _IS_WINDOWS,
    CLIConfig,
    _win_feed_stdin,
)
from ductor_bot.cli.stream_events import ResultEvent, StreamEvent, SystemStatusEvent
from ductor_bot.cli.types import CLIResponse
from ductor_bot.infra.process_tree import force_kill_process_tree

logger = logging.getLogger(__name__)

_DEFAULT_DYNAMIC_TIMEOUT_MAX_SECONDS = 3600.0
_DYNAMIC_TIMEOUT_MAX_ENV = "DUCTOR_DYNAMIC_TIMEOUT_MAX_SECONDS"
_STREAM_TIMEOUT_POLL_SECONDS = 5.0
_TIMEOUT_WARN_60_SECONDS = 60.0
_TIMEOUT_WARN_10_SECONDS = 10.0


def _resolve_dynamic_timeout_max(timeout_seconds: float | None) -> float | None:
    """Return the dynamic maximum timeout cap for one stream.

    Priority:
    1) ``DUCTOR_DYNAMIC_TIMEOUT_MAX_SECONDS`` env var (if valid positive float)
    2) default: max(configured timeout, 1 hour)
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        return None

    raw = os.environ.get(_DYNAMIC_TIMEOUT_MAX_ENV, "").strip()
    if raw:
        try:
            configured = float(raw)
        except ValueError:
            logger.warning("Invalid %s='%s' (must be number)", _DYNAMIC_TIMEOUT_MAX_ENV, raw)
        else:
            if configured > 0:
                return max(timeout_seconds, configured)
            logger.warning("%s must be > 0, got '%s'", _DYNAMIC_TIMEOUT_MAX_ENV, raw)
    return max(timeout_seconds, _DEFAULT_DYNAMIC_TIMEOUT_MAX_SECONDS)


def _timeout_result_text(elapsed_seconds: float, base_timeout: float | None, cap_seconds: float | None) -> str:
    """Build a user-facing timeout message (English by default)."""
    elapsed_label = f"{elapsed_seconds:.0f}s"
    base_label = f"{base_timeout:.0f}s" if base_timeout and base_timeout > 0 else "configured window"
    if cap_seconds and cap_seconds > 0:
        cap_label = f"{cap_seconds:.0f}s"
        return (
            f"Request timed out after {elapsed_label}. "
            f"The timeout window started at {base_label} and was auto-extended up to {cap_label}.\n"
            "If this is expected to run longer, try again and ask to continue from the previous state."
        )
    return (
        f"Request timed out after {elapsed_label} (configured timeout: {base_label}).\n"
        "If this is expected to run longer, try again and ask to continue from the previous state."
    )


def _build_subprocess_env(config: CLIConfig) -> dict[str, str] | None:
    """Build environment dict with agent identification vars.

    Returns None if no extra vars are needed (avoids inheriting a stripped env).
    For non-Docker execution, the subprocess inherits the parent env plus the
    agent identification variables.
    """
    import os
    from pathlib import Path

    env = os.environ.copy()
    env["DUCTOR_AGENT_NAME"] = config.agent_name
    env["DUCTOR_AGENT_ROLE"] = "main" if config.agent_name == "main" else "sub"
    env["DUCTOR_INTERAGENT_PORT"] = str(config.interagent_port)
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
    """What to run: command, working directory, prompt, and timeout."""

    exec_cmd: list[str]
    use_cwd: str | None
    prompt: str
    timeout_seconds: float | None = None


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
    2. Feed stdin on Windows (prompt via pipe)
    3. Register in process registry
    4. Drain stderr in background task
    5. Stream stdout lines through *line_handler* with timeout
    6. On timeout: kill, yield error, return
    7. Cleanup: cancel drain, unregister tracked process
    8. Post-loop: delegate to *post_handler* (default: yield error on non-zero exit)
    """
    subprocess_env = _build_subprocess_env(config) if spec.use_cwd else None
    process = await asyncio.create_subprocess_exec(
        *spec.exec_cmd,
        stdin=_win_stdin_pipe(),
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
    _win_feed_stdin(process, spec.prompt)
    logger.info("%s subprocess starting pid=%s", provider_label, process.pid)

    reg = config.process_registry
    tracked = reg.register(config.chat_id, process, config.process_label) if reg else None
    stderr_drain = asyncio.create_task(process.stderr.read())

    base_timeout = spec.timeout_seconds
    dynamic_timeout_cap = _resolve_dynamic_timeout_max(base_timeout)
    stream_started_at = time.monotonic()
    deadline = (
        stream_started_at + base_timeout if base_timeout is not None and base_timeout > 0 else None
    )
    warned_60 = False
    warned_10 = False

    try:
        while True:
            if deadline is None:
                line_bytes = await process.stdout.readline()
            else:
                while True:
                    now = time.monotonic()
                    elapsed = now - stream_started_at
                    remaining = deadline - now

                    if remaining <= 0:
                        if dynamic_timeout_cap is not None and elapsed < dynamic_timeout_cap:
                            extended_deadline = min(
                                stream_started_at + dynamic_timeout_cap,
                                deadline + (base_timeout or 0),
                            )
                            if extended_deadline > deadline:
                                deadline = extended_deadline
                                warned_60 = False
                                warned_10 = False
                                logger.info(
                                    "%s stream timeout auto-extended elapsed=%.0fs cap=%.0fs",
                                    provider_label,
                                    elapsed,
                                    dynamic_timeout_cap,
                                )
                                yield SystemStatusEvent(
                                    type="system",
                                    subtype="status",
                                    status="timeout_extended",
                                )
                                continue
                        raise TimeoutError

                    if remaining <= _TIMEOUT_WARN_60_SECONDS and not warned_60:
                        warned_60 = True
                        yield SystemStatusEvent(
                            type="system",
                            subtype="status",
                            status="timeout_warn_60",
                        )
                    if remaining <= _TIMEOUT_WARN_10_SECONDS and not warned_10:
                        warned_10 = True
                        yield SystemStatusEvent(
                            type="system",
                            subtype="status",
                            status="timeout_warn_10",
                        )

                    poll_for = min(remaining, _STREAM_TIMEOUT_POLL_SECONDS)
                    try:
                        line_bytes = await asyncio.wait_for(process.stdout.readline(), timeout=poll_for)
                        break
                    except TimeoutError:
                        continue

            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace").rstrip()
            logger.debug("Stream line: %s", line[:120])
            async for event in line_handler(line):
                yield event
        stderr_bytes = await stderr_drain
    except TimeoutError:
        force_kill_process_tree(process.pid)
        await process.wait()
        elapsed_seconds = max(0.0, time.monotonic() - stream_started_at)
        logger.warning(
            "%s stream timed out elapsed=%.0fs base=%.0fs cap=%.0fs",
            provider_label,
            elapsed_seconds,
            base_timeout or 0.0,
            dynamic_timeout_cap or 0.0,
        )
        yield ResultEvent(
            type="result",
            result=_timeout_result_text(elapsed_seconds, base_timeout, dynamic_timeout_cap),
            is_error=True,
            returncode=124,
        )
        return
    finally:
        await _cancel_drain(stderr_drain)
        if tracked and reg:
            reg.unregister(tracked)

    await process.wait()

    handler = post_handler or _default_post_handler
    async for event in handler(SubprocessResult(process=process, stderr_bytes=stderr_bytes)):
        yield event


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
    2. Communicate (stdin on Windows + wait)
    3. Register/unregister in process registry
    4. Handle timeout
    5. Parse output via *parse_output* callback
    """
    oneshot_env = _build_subprocess_env(config) if spec.use_cwd else None
    process = await asyncio.create_subprocess_exec(
        *spec.exec_cmd,
        stdin=_win_stdin_pipe(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=spec.use_cwd,
        env=oneshot_env,
        creationflags=_CREATION_FLAGS,
    )
    logger.info("%s subprocess starting pid=%s", provider_label, process.pid)

    reg = config.process_registry
    tracked = reg.register(config.chat_id, process, config.process_label) if reg else None
    try:
        stdin_data = spec.prompt.encode() if _IS_WINDOWS else None
        async with asyncio.timeout(spec.timeout_seconds):
            stdout, stderr = await process.communicate(input=stdin_data)
    except TimeoutError:
        force_kill_process_tree(process.pid)
        await process.wait()
        logger.warning("%s timed out after %.0fs", provider_label, spec.timeout_seconds)
        return CLIResponse(
            result=f"Request timed out after {spec.timeout_seconds:.0f}s.",
            is_error=True,
            timed_out=True,
        )
    finally:
        if tracked and reg:
            reg.unregister(tracked)

    return parse_output(stdout, stderr, process.returncode)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _win_stdin_pipe() -> int | None:
    """Return ``asyncio.subprocess.PIPE`` on Windows, else ``None``."""
    return asyncio.subprocess.PIPE if _IS_WINDOWS else None


async def _cancel_drain(drain: asyncio.Task[bytes]) -> None:
    """Cancel a stderr drain task and silently absorb any resulting exception."""
    if not drain.done():
        drain.cancel()
        with contextlib.suppress(BaseException):
            await drain
