"""Async wrapper around the Claude Code CLI."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from shutil import which

from ductor_bot.cli.base import BaseCLI, CLIConfig, docker_wrap
from ductor_bot.cli.stream_events import (
    ResultEvent,
    StreamEvent,
    parse_stream_line,
)
from ductor_bot.cli.types import CLIResponse

logger = logging.getLogger(__name__)


class ClaudeCodeCLI(BaseCLI):
    """Async wrapper around the Claude Code CLI."""

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = "claude" if config.docker_container else self._find_cli()
        logger.info("CLI wrapper: cwd=%s, model=%s", self._working_dir, config.model)

    @staticmethod
    def _find_cli() -> str:
        path = which("claude")
        if not path:
            msg = (
                "claude CLI not found on PATH. "
                "Install via: npm install -g @anthropic-ai/claude-code"
            )
            raise FileNotFoundError(msg)
        return path

    def _build_command(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
    ) -> list[str]:
        cfg = self._config
        cmd = [self._cli, "-p", "--output-format", "json"]

        _add_opt(cmd, "--permission-mode", cfg.permission_mode)
        _add_opt(cmd, "--model", cfg.model)
        _add_opt(cmd, "--system-prompt", cfg.system_prompt)
        _add_opt(cmd, "--append-system-prompt", cfg.append_system_prompt)
        _add_opt(cmd, "--max-turns", str(cfg.max_turns) if cfg.max_turns is not None else None)
        _add_opt(
            cmd,
            "--max-budget-usd",
            str(cfg.max_budget_usd) if cfg.max_budget_usd is not None else None,
        )

        if cfg.allowed_tools:
            cmd += ["--allowedTools", *cfg.allowed_tools]
        if cfg.disallowed_tools:
            cmd += ["--disallowedTools", *cfg.disallowed_tools]

        if resume_session:
            cmd += ["--resume", resume_session]
        elif continue_session:
            cmd.append("--continue")

        cmd.append("--")
        cmd.append(prompt)
        return cmd

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
    ) -> CLIResponse:
        """Send a prompt and return the final result."""
        cmd = self._build_command(prompt, resume_session, continue_session)
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
        logger.info("CLI subprocess starting pid=%s", process.pid)

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
            logger.warning("CLI timed out after %.0fs", timeout_seconds)
            return CLIResponse(result="", is_error=True, timed_out=True)
        finally:
            if tracked and reg:
                reg.unregister(tracked)

        return _parse_response(stdout, stderr, process.returncode)

    def _build_command_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
    ) -> list[str]:
        """Build CLI command with --output-format stream-json."""
        cmd = self._build_command(prompt, resume_session, continue_session)
        try:
            idx = cmd.index("json")
            cmd[idx] = "stream-json"
        except ValueError:
            pass
        if "--verbose" not in cmd:
            cmd.insert(1, "--verbose")
        return cmd

    async def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Send a prompt and yield stream events as they arrive."""
        cmd = self._build_command_streaming(prompt, resume_session, continue_session)
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
        logger.info("CLI subprocess starting pid=%s", process.pid)

        reg = self._config.process_registry
        tracked = (
            reg.register(self._config.chat_id, process, self._config.process_label) if reg else None
        )
        stderr_drain = asyncio.create_task(process.stderr.read())
        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    line_bytes = await process.stdout.readline()
                    if not line_bytes:
                        break
                    line = line_bytes.decode(errors="replace").rstrip()
                    logger.debug("Stream line: %s", line[:120])
                    for event in parse_stream_line(line):
                        yield event
        except TimeoutError:
            process.kill()
            await process.wait()
            logger.warning("CLI stream timed out after %.0fs", timeout_seconds)
            yield ResultEvent(type="result", result="", is_error=True)
            return
        finally:
            if tracked and reg:
                reg.unregister(tracked)

        stderr_bytes = await stderr_drain
        await process.wait()
        stderr_text = stderr_bytes.decode(errors="replace")[:2000] if stderr_bytes else ""

        if process.returncode != 0:
            logger.warning(
                "CLI stream exited with code %d: %s",
                process.returncode,
                stderr_text[:200] if stderr_text else "(no stderr)",
            )
            yield ResultEvent(type="result", result=stderr_text[:500], is_error=True)


def _add_opt(cmd: list[str], flag: str, value: str | None) -> None:
    """Append a CLI flag+value pair if value is truthy."""
    if value:
        cmd += [flag, value]


def _log_cmd(cmd: list[str], *, streaming: bool = False) -> None:
    """Log the CLI command with truncated long values."""
    safe_cmd = [
        (c[:80] + "...") if len(c) > 80 and i > 0 and cmd[i - 1].startswith("--") else c
        for i, c in enumerate(cmd)
    ]
    prefix = "CLI stream cmd" if streaming else "CLI cmd"
    logger.info("%s: %s", prefix, " ".join(safe_cmd))


def _parse_response(stdout: bytes, stderr: bytes, returncode: int | None) -> CLIResponse:
    """Parse CLI subprocess output into a CLIResponse."""
    stderr_text = stderr.decode(errors="replace")[:2000] if stderr else ""
    if stderr_text:
        logger.warning("CLI stderr: %s", stderr_text[:500])

    raw = stdout.decode().strip()
    if not raw:
        logger.error("CLI returned empty output (exit=%s)", returncode)
        return CLIResponse(result="", is_error=True, returncode=returncode, stderr=stderr_text)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception("Failed to parse CLI JSON: %s", raw[:500])
        return CLIResponse(result=raw, is_error=True, returncode=returncode, stderr=stderr_text)

    response = CLIResponse(
        session_id=data.get("session_id"),
        result=data.get("result", ""),
        is_error=data.get("is_error", False),
        returncode=returncode,
        stderr=stderr_text,
        duration_ms=data.get("duration_ms"),
        duration_api_ms=data.get("duration_api_ms"),
        num_turns=data.get("num_turns"),
        total_cost_usd=data.get("total_cost_usd"),
        usage=data.get("usage", {}),
        model_usage=data.get("modelUsage", {}),
    )

    if response.is_error:
        logger.error("CLI error: %s", response.result[:200])
    else:
        logger.info(
            "CLI done session=%s turns=%s cost=$%.4f tokens=%d duration_ms=%.0f",
            (response.session_id or "?")[:8],
            response.num_turns,
            response.total_cost_usd or 0,
            response.total_tokens,
            response.duration_ms or 0,
        )

    return response
