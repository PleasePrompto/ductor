"""Async wrapper around the OpenCode CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.base import (
    BaseCLI,
    CLIConfig,
    _feed_stdin_and_close,
    docker_wrap,
)
from ductor_bot.cli.opencode_events import parse_opencode_stream_line
from ductor_bot.cli.stream_events import ResultEvent, StreamEvent, SystemInitEvent
from ductor_bot.cli.types import CLIResponse
from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

if TYPE_CHECKING:
    from ductor_bot.cli.process_registry import ProcessRegistry, TrackedProcess
    from ductor_bot.cli.timeout_controller import TimeoutController

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 600.0


def find_opencode_cli() -> str:
    """Find the opencode binary."""
    path = shutil.which("opencode")
    if path:
        return path
    raise FileNotFoundError("opencode CLI not found in PATH")


@dataclass(slots=True)
class _OpenCodeStreamState:
    """Mutable stream-state for OpenCode event processing."""

    last_session_id: str | None
    saw_result: bool = False

    def track(self, event: StreamEvent) -> None:
        if isinstance(event, (SystemInitEvent, ResultEvent)) and event.session_id:
            self.last_session_id = event.session_id
        if isinstance(event, ResultEvent):
            self.saw_result = True
            if not event.session_id:
                event.session_id = self.last_session_id


class OpenCodeCLI(BaseCLI):
    """Async wrapper around the OpenCode CLI."""

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = find_opencode_cli()
        logger.info("OpenCodeCLI: cwd=%s model=%s", self._working_dir, config.model)

    def _build_command(
        self,
        *,
        resume_session: str | None = None,
        continue_session: bool = False,
    ) -> list[str]:
        """Build the CLI command list."""
        cfg = self._config
        cmd = ["opencode", "run"]

        if cfg.model:
            cmd += ["-m", cfg.model]

        if cfg.permission_mode == "bypassPermissions":
            cmd.append("--dangerously-skip-permissions")

        if resume_session:
            cmd += ["-s", resume_session]
        elif continue_session:
            cmd.append("-c")

        cmd.extend(["--format", "json"])

        if cfg.cli_parameters:
            cmd.extend(cfg.cli_parameters)

        return cmd

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> CLIResponse:
        """Execute a non-streaming OpenCode CLI call."""
        cmd = self._build_command(resume_session=resume_session, continue_session=continue_session)
        return await self._execute_oneshot(cmd, prompt, timeout_seconds, timeout_controller)

    def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream events from OpenCode CLI."""
        cmd = self._build_command(resume_session=resume_session, continue_session=continue_session)
        return self._stream_events(cmd, prompt, timeout_seconds, timeout_controller)

    async def _execute_oneshot(
        self,
        cmd: list[str],
        prompt: str,
        timeout_seconds: float | None,
        timeout_controller: TimeoutController | None,
    ) -> CLIResponse:
        """Run oneshot subprocess and parse JSON output."""
        exec_cmd, use_cwd = docker_wrap(cmd, self._config)
        logger.info("OpenCode cmd: %s", " ".join(cmd))

        process = await asyncio.create_subprocess_exec(
            *exec_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=use_cwd,
            creationflags=_CREATION_FLAGS,
        )

        reg, tracked = self._track_process(process)
        try:
            stdout, stderr = await self._communicate_with_timeout(
                process,
                prompt,
                timeout_seconds=timeout_seconds or _DEFAULT_TIMEOUT,
                timeout_controller=timeout_controller,
            )
        except TimeoutError:
            logger.warning("OpenCode send timed out")
            force_kill_process_tree(process.pid)
            stdout, stderr = await process.communicate()
            return CLIResponse(
                result="Timeout",
                is_error=True,
                timed_out=True,
                returncode=process.returncode,
                stderr=stderr.decode(errors="replace")[:2000] if stderr else "",
            )
        finally:
            self._untrack_process(reg, tracked)

        return self._parse_response(stdout, stderr, process.returncode)

    async def _communicate_with_timeout(
        self,
        process: asyncio.subprocess.Process,
        prompt: str,
        *,
        timeout_seconds: float,
        timeout_controller: TimeoutController | None,
    ) -> tuple[bytes, bytes]:
        """Run process.communicate with optional timeout controller."""
        communicate_coro = process.communicate(input=prompt.encode())
        if timeout_controller is not None:
            return await timeout_controller.run_with_timeout(communicate_coro)
        async with asyncio.timeout(timeout_seconds):
            return await communicate_coro

    async def _stream_events(
        self,
        cmd: list[str],
        prompt: str,
        timeout_seconds: float | None,
        timeout_controller: TimeoutController | None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream OpenCode output line by line."""
        exec_cmd, use_cwd = docker_wrap(cmd, self._config)
        logger.info("OpenCode stream cmd: %s", " ".join(cmd))

        process = await asyncio.create_subprocess_exec(
            *exec_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=use_cwd,
            limit=4 * 1024 * 1024,
            creationflags=_CREATION_FLAGS,
        )

        if process.stderr is None:
            msg = "OpenCode subprocess created without stderr pipe"
            raise RuntimeError(msg)

        stderr_task = asyncio.create_task(process.stderr.read())
        reg, tracked = self._track_process(process)
        state = _OpenCodeStreamState(last_session_id=None)
        timed_out = False

        try:
            await _feed_stdin_and_close(process, prompt)
            async for event in self._read_stream(
                process,
                state,
                timeout_seconds or _DEFAULT_TIMEOUT,
                timeout_controller,
            ):
                yield event
        except TimeoutError:
            timed_out = True
            yield ResultEvent(
                type="result",
                result=f"__TIMEOUT__{int(timeout_seconds or _DEFAULT_TIMEOUT)}",
                is_error=True,
                session_id=state.last_session_id,
            )
        finally:
            stderr_bytes = await self._finish_process(process, stderr_task)
            self._untrack_process(reg, tracked)

        final_event = self._build_exit_event(process.returncode, stderr_bytes, state)
        if final_event is not None and not timed_out:
            was_aborted = bool(reg and reg.was_aborted(self._config.chat_id))
            if not was_aborted:
                yield final_event

    async def _read_stream(
        self,
        process: asyncio.subprocess.Process,
        state: _OpenCodeStreamState,
        timeout_seconds: float,
        timeout_controller: TimeoutController | None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Read stdout lines and yield parsed events."""
        if process.stdout is None:
            raise RuntimeError("OpenCode subprocess created without stdout pipe")

        reg = self._config.process_registry
        chat_id = self._config.chat_id

        if timeout_controller is not None:
            async for event in self._read_stream_with_controller(
                process, state, timeout_controller, reg, chat_id
            ):
                yield event
        else:
            async for event in self._read_stream_plain(
                process, state, timeout_seconds, reg, chat_id
            ):
                yield event

    async def _read_stream_with_controller(
        self,
        process: asyncio.subprocess.Process,
        state: _OpenCodeStreamState,
        timeout_controller: TimeoutController,
        reg: ProcessRegistry | None,
        chat_id: int,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Read stream with managed timeout extensions + warnings."""
        assert process.stdout is not None
        timeout_controller.begin()
        warning_task = timeout_controller.start_warning_loop()
        timeout_secs = timeout_controller.timeout_seconds
        try:
            while True:
                try:
                    async with asyncio.timeout(timeout_secs):
                        while True:
                            if reg and reg.was_aborted(chat_id):
                                logger.info("OpenCode streaming aborted by user")
                                return

                            line_bytes = await process.stdout.readline()
                            if not line_bytes:
                                return
                            timeout_controller.record_activity()

                            line = line_bytes.decode(errors="replace").rstrip()
                            if not line:
                                continue

                            logger.debug("OpenCode raw line: %.200s", line)
                            for event in parse_opencode_stream_line(line):
                                state.track(event)
                                yield event
                except TimeoutError:
                    if timeout_controller.try_extend():
                        timeout_secs = timeout_controller.activity_extension_seconds
                        continue
                    raise
        finally:
            if warning_task and not warning_task.done():
                warning_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await warning_task

    async def _read_stream_plain(
        self,
        process: asyncio.subprocess.Process,
        state: _OpenCodeStreamState,
        timeout_seconds: float,
        reg: ProcessRegistry | None,
        chat_id: int,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Read stream with a fixed timeout (legacy behavior)."""
        assert process.stdout is not None
        async with asyncio.timeout(timeout_seconds):
            while True:
                if reg and reg.was_aborted(chat_id):
                    logger.info("OpenCode streaming aborted by user")
                    return

                line_bytes = await process.stdout.readline()
                if not line_bytes:
                    return

                line = line_bytes.decode(errors="replace").rstrip()
                if not line:
                    continue

                logger.debug("OpenCode raw line: %.200s", line)
                for event in parse_opencode_stream_line(line):
                    state.track(event)
                    yield event

    async def _finish_process(
        self, process: asyncio.subprocess.Process, stderr_task: asyncio.Task[bytes]
    ) -> bytes:
        """Ensure process shutdown and return collected stderr."""
        if process.returncode is None:
            force_kill_process_tree(process.pid)
        await process.wait()
        return await stderr_task

    def _build_exit_event(
        self, returncode: int | None, stderr_bytes: bytes, state: _OpenCodeStreamState
    ) -> ResultEvent | None:
        """Build a synthetic final ResultEvent when the stream lacked one."""
        if state.saw_result:
            return None

        if returncode == 0:
            return ResultEvent(
                type="result",
                result="",
                is_error=False,
                returncode=returncode,
                session_id=state.last_session_id,
            )

        detail = stderr_bytes.decode(errors="replace").strip()
        if not detail:
            detail = f"OpenCode exited with code {returncode}"
        return ResultEvent(
            type="result",
            result=detail[:500],
            is_error=True,
            returncode=returncode,
            session_id=state.last_session_id,
        )

    def _track_process(
        self, process: asyncio.subprocess.Process
    ) -> tuple[ProcessRegistry | None, TrackedProcess | None]:
        """Register a subprocess in ProcessRegistry if tracking is enabled."""
        reg = self._config.process_registry
        tracked = (
            reg.register(
                self._config.chat_id,
                process,
                self._config.process_label,
                topic_id=self._config.topic_id,
            )
            if reg
            else None
        )
        return reg, tracked

    @staticmethod
    def _untrack_process(reg: ProcessRegistry | None, tracked: TrackedProcess | None) -> None:
        """Unregister a previously tracked subprocess."""
        if tracked is not None and reg is not None:
            reg.unregister(tracked)

    def _parse_response(self, stdout: bytes, stderr: bytes, returncode: int | None) -> CLIResponse:
        """Parse OpenCode CLI JSON output into CLIResponse."""
        from ductor_bot.cli.opencode_events import parse_opencode_json

        stderr_text = stderr.decode(errors="replace")[:2000] if stderr else ""
        raw = stdout.decode(errors="replace").strip()

        if not raw:
            return CLIResponse(result="", is_error=True, returncode=returncode, stderr=stderr_text)

        result_text = parse_opencode_json(raw)
        is_error = returncode != 0

        if not result_text:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("error"):
                    is_error = True
                    result_text = str(parsed.get("error"))
            except json.JSONDecodeError:
                result_text = raw[:2000]

        return CLIResponse(
            session_id=None,
            result=result_text or raw[:2000],
            is_error=is_error,
            returncode=returncode,
            stderr=stderr_text,
        )
