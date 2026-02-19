"""Async wrapper around the OpenAI Codex CLI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from shutil import which

from ductor_bot.cli.base import BaseCLI, CLIConfig, docker_wrap
from ductor_bot.cli.codex_events import (
    CodexThinkingFilter,
    parse_codex_jsonl,
    parse_codex_stream_event,
)
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
    SystemInitEvent,
)
from ductor_bot.cli.types import CLIResponse

logger = logging.getLogger(__name__)


class _StreamState:
    """Mutable accumulator for streaming session data."""

    __slots__ = ("accumulated_text", "thread_id")

    def __init__(self) -> None:
        self.accumulated_text: list[str] = []
        self.thread_id: str | None = None

    def track(self, event: StreamEvent) -> None:
        """Update state from a single stream event."""
        if isinstance(event, SystemInitEvent) and event.session_id:
            self.thread_id = event.session_id
        elif isinstance(event, AssistantTextDelta) and event.text:
            self.accumulated_text.append(event.text)


class CodexCLI(BaseCLI):
    """Async wrapper around the OpenAI Codex CLI."""

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = "codex" if config.docker_container else self._find_cli()
        logger.info("Codex CLI wrapper: cwd=%s, model=%s", self._working_dir, config.model)

    @staticmethod
    def _find_cli() -> str:
        path = which("codex")
        if not path:
            msg = "codex CLI not found on PATH. Install via: npm install -g @openai/codex"
            raise FileNotFoundError(msg)
        return path

    def _compose_prompt(self, prompt: str) -> str:
        """Inject system context into user prompt (Codex has no --system-prompt)."""
        cfg = self._config
        parts: list[str] = []
        if cfg.system_prompt:
            parts.append(cfg.system_prompt)
        parts.append(prompt)
        if cfg.append_system_prompt:
            parts.append(cfg.append_system_prompt)
        return "\n\n".join(parts)

    def _sandbox_flags(self) -> list[str]:
        """Return sandbox/approval flags based on permission_mode."""
        cfg = self._config
        if cfg.permission_mode == "bypassPermissions":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        if cfg.sandbox_mode == "full-access":
            return ["--sandbox", "danger-full-access"]
        if cfg.sandbox_mode == "workspace-write":
            return ["--full-auto"]
        return ["--sandbox", cfg.sandbox_mode]

    def _build_command(
        self,
        prompt: str,
        resume_session: str | None = None,
        *,
        json_output: bool = True,
    ) -> list[str]:
        cfg = self._config
        final_prompt = self._compose_prompt(prompt)

        if resume_session:
            resume_cmd = [self._cli, "exec", "resume"]
            if json_output:
                resume_cmd.append("--json")
            resume_cmd += self._sandbox_flags()
            resume_cmd += ["--", resume_session, final_prompt]
            return resume_cmd

        cmd = [self._cli, "exec"]
        if json_output:
            cmd.append("--json")
        cmd += ["--color", "never"]
        cmd += self._sandbox_flags()
        cmd.append("--skip-git-repo-check")

        if cfg.model:
            cmd += ["--model", cfg.model]
        if cfg.reasoning_effort and cfg.reasoning_effort != "default":
            cmd += ["-c", f"model_reasoning_effort={cfg.reasoning_effort}"]
        if cfg.instructions:
            cmd += ["--instructions", cfg.instructions]
        for img in cfg.images:
            cmd += ["--image", img]

        # Add extra CLI parameters before the separator
        if cfg.cli_parameters:
            cmd.extend(cfg.cli_parameters)

        cmd.append("--")
        cmd.append(final_prompt)
        return cmd

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
    ) -> CLIResponse:
        """Send a prompt and return the final result."""
        if continue_session:
            logger.debug("continue_session is not supported by Codex CLI, ignoring")
        cmd = self._build_command(prompt, resume_session, json_output=True)
        _log_cmd(cmd)

        exec_cmd, use_cwd = docker_wrap(
            cmd, self._config.docker_container, self._config.chat_id, self._working_dir
        )
        process = await asyncio.create_subprocess_exec(
            *exec_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=use_cwd,
        )
        logger.info("Codex subprocess starting pid=%s", process.pid)

        reg = self._config.process_registry
        tracked = (
            reg.register(self._config.chat_id, process, self._config.process_label) if reg else None
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            logger.warning("Codex CLI timed out after %.0fs", timeout_seconds)
            return CLIResponse(result="", is_error=True, timed_out=True)
        finally:
            if tracked and reg:
                reg.unregister(tracked)

        return self._parse_output(stdout, stderr, process.returncode)

    async def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Send a prompt and yield stream events as they arrive."""
        cmd = self._build_command(prompt, resume_session, json_output=True)
        _log_cmd(cmd, streaming=True)

        exec_cmd, use_cwd = docker_wrap(
            cmd, self._config.docker_container, self._config.chat_id, self._working_dir
        )
        process = await asyncio.create_subprocess_exec(
            *exec_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=use_cwd,
            limit=4 * 1024 * 1024,
        )
        if process.stdout is None or process.stderr is None:
            msg = "Subprocess created without stdout/stderr pipes"
            raise RuntimeError(msg)
        logger.info("Codex subprocess starting pid=%s", process.pid)

        reg = self._config.process_registry
        tracked = (
            reg.register(self._config.chat_id, process, self._config.process_label) if reg else None
        )
        state = _StreamState()
        thinking_filter = CodexThinkingFilter()
        stderr_drain = asyncio.create_task(process.stderr.read())

        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    line_bytes = await process.stdout.readline()
                    if not line_bytes:
                        break
                    line = line_bytes.decode(errors="replace").rstrip()
                    if not line:
                        continue

                    logger.debug("Stream line: %s", line[:120])
                    for raw_event in parse_codex_stream_event(line):
                        for event in thinking_filter.process(raw_event):
                            state.track(event)
                            yield event
                for event in thinking_filter.flush():
                    state.track(event)
                    yield event
            # Normal end-of-stream: collect stderr now while still in the try
            # block so the finally clause can cancel the drain task if needed.
            stderr_bytes = await stderr_drain
        except TimeoutError:
            process.kill()
            await process.wait()
            logger.warning("Codex stream timed out after %.0fs", timeout_seconds)
            yield ResultEvent(type="result", result="", is_error=True)
            return
        finally:
            await _cancel_drain(stderr_drain)
            if tracked and reg:
                reg.unregister(tracked)

        yield await _codex_final_result(
            process, state.accumulated_text, state.thread_id, stderr_bytes
        )

    @staticmethod
    def _parse_output(
        stdout: bytes,
        stderr: bytes,
        returncode: int | None,
    ) -> CLIResponse:
        """Parse Codex subprocess output into a CLIResponse."""
        stderr_text = stderr.decode(errors="replace")[:2000] if stderr else ""
        if stderr_text:
            logger.warning("Codex stderr (exit=%s): %s", returncode, stderr_text[:500])

        raw = stdout.decode(errors="replace").strip()
        if not raw:
            logger.error("Codex returned empty output (exit=%s)", returncode)
            return CLIResponse(result="", is_error=True, returncode=returncode, stderr=stderr_text)

        is_error = returncode != 0
        result_text, thread_id, usage = parse_codex_jsonl(raw)
        response = CLIResponse(
            session_id=thread_id,
            result=result_text or raw,
            is_error=is_error or not result_text,
            returncode=returncode,
            stderr=stderr_text,
            usage=usage or {},
        )

        if response.is_error:
            logger.error("Codex error exit=%s: %s", returncode, response.result[:300])
        else:
            logger.info(
                "Codex done session=%s tokens=%d",
                (response.session_id or "?")[:8],
                response.total_tokens,
            )

        return response


async def _cancel_drain(drain: asyncio.Task[bytes]) -> None:
    """Cancel a stderr drain task and silently absorb any resulting exception."""
    if not drain.done():
        drain.cancel()
        with contextlib.suppress(BaseException):
            await drain


async def _codex_final_result(
    process: asyncio.subprocess.Process,
    accumulated_text: list[str],
    thread_id: str | None,
    stderr_bytes: bytes = b"",
) -> ResultEvent:
    """Build the final ResultEvent after the stream loop completes."""
    await process.wait()
    stderr_text = stderr_bytes.decode(errors="replace")[:2000] if stderr_bytes else ""

    if process.returncode != 0:
        error_detail = stderr_text or "\n".join(accumulated_text) or "(no output)"
        logger.error(
            "Codex stream exited with code %d: %s",
            process.returncode,
            error_detail[:300],
        )
        return ResultEvent(
            type="result", result=error_detail[:500], is_error=True, returncode=process.returncode
        )

    return ResultEvent(
        type="result",
        session_id=thread_id,
        result="\n".join(accumulated_text),
        is_error=False,
        returncode=process.returncode,
    )


def _log_cmd(cmd: list[str], *, streaming: bool = False) -> None:
    """Log the CLI command with truncated long values."""
    safe_cmd = [(c[:80] + "...") if len(c) > 80 else c for c in cmd]
    prefix = "Codex stream cmd" if streaming else "Codex cmd"
    logger.info("%s: %s", prefix, " ".join(safe_cmd))
