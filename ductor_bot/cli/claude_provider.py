"""Async wrapper around the Claude Code CLI."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING

from ductor_bot.cli.base import (
    _IS_WINDOWS,
    BaseCLI,
    CLIConfig,
    _cleanup_file,
    add_cli_opt,
    docker_prompt_tmp_dir,
    docker_wrap,
    format_cli_cmd,
    host_path_to_container,
)
from ductor_bot.cli.executor import SubprocessSpec, run_oneshot_subprocess, run_streaming_subprocess
from ductor_bot.cli.gemini_utils import create_system_prompt_file
from ductor_bot.cli.stream_events import (
    StreamEvent,
    parse_stream_line,
)
from ductor_bot.cli.types import CLIResponse

if TYPE_CHECKING:
    from ductor_bot.cli.timeout_controller import TimeoutController

logger = logging.getLogger(__name__)

# Claude passes ``--append-system-prompt`` as a single argv token.  Linux caps
# one argument at ``MAX_ARG_STRLEN`` (128 KiB); a larger value makes
# ``execve`` fail with ``OSError`` E2BIG.  Above this (byte) threshold the prompt
# is written to a temp file and passed via ``--append-system-prompt-file``.
_MAX_INLINE_APPEND_BYTES = 96 * 1024
_APPEND_PREFIX = "ductor_append_"


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
        *,
        append_prompt_file: str | None = None,
    ) -> list[str]:
        cfg = self._config
        cmd = [self._cli, "-p", "--output-format", "json"]

        add_cli_opt(cmd, "--permission-mode", cfg.permission_mode)
        add_cli_opt(cmd, "--model", cfg.model)
        if cfg.reasoning_effort and cfg.reasoning_effort != "default":
            cmd += ["--effort", cfg.reasoning_effort]
        add_cli_opt(cmd, "--system-prompt", cfg.system_prompt)
        if append_prompt_file:
            cmd += ["--append-system-prompt-file", append_prompt_file]
        else:
            add_cli_opt(cmd, "--append-system-prompt", cfg.append_system_prompt)
        add_cli_opt(cmd, "--max-turns", str(cfg.max_turns) if cfg.max_turns is not None else None)
        add_cli_opt(
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

        # Add extra CLI parameters before the separator
        if cfg.cli_parameters:
            cmd.extend(cfg.cli_parameters)

        # On Windows, .CMD wrappers mangle arguments with special characters.
        # The prompt is passed via stdin instead (see send / send_streaming).
        if not _IS_WINDOWS:
            cmd.append("--")
            cmd.append(prompt)
        return cmd

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> CLIResponse:
        """Send a prompt and return the final result."""
        append_file = self._create_append_prompt_path()
        try:
            cmd = self._build_command(
                prompt,
                resume_session,
                continue_session,
                append_prompt_file=self._append_arg_path(append_file),
            )
            exec_cmd, use_cwd = docker_wrap(cmd, self._config, interactive=_IS_WINDOWS)
            _log_cmd(exec_cmd)
            return await run_oneshot_subprocess(
                config=self._config,
                spec=SubprocessSpec(exec_cmd, use_cwd, prompt, timeout_seconds, timeout_controller),
                parse_output=_parse_response,
                provider_label="CLI",
            )
        finally:
            await _cleanup_file(append_file)

    def _build_command_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        *,
        append_prompt_file: str | None = None,
    ) -> list[str]:
        """Build CLI command with --output-format stream-json."""
        cmd = self._build_command(
            prompt, resume_session, continue_session, append_prompt_file=append_prompt_file
        )
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
        timeout_controller: TimeoutController | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Send a prompt and yield stream events as they arrive."""
        append_file = self._create_append_prompt_path()
        try:
            cmd = self._build_command_streaming(
                prompt,
                resume_session,
                continue_session,
                append_prompt_file=self._append_arg_path(append_file),
            )
            exec_cmd, use_cwd = docker_wrap(cmd, self._config, interactive=_IS_WINDOWS)
            _log_cmd(exec_cmd, streaming=True)

            async for event in run_streaming_subprocess(
                config=self._config,
                spec=SubprocessSpec(exec_cmd, use_cwd, prompt, timeout_seconds, timeout_controller),
                line_handler=_claude_line_handler,
                provider_label="CLI",
            ):
                yield event
        finally:
            await _cleanup_file(append_file)

    def _create_append_prompt_path(self) -> str | None:
        """Write an oversized ``--append-system-prompt`` to a temp file.

        Returns the host path, or ``None`` when the prompt is empty or small
        enough to pass inline.  A value above ``_MAX_INLINE_APPEND_BYTES`` would
        exceed the kernel's per-argument limit and crash ``execve`` with
        ``OSError`` E2BIG, so it is handed to the CLI via
        ``--append-system-prompt-file`` instead.  The caller must clean up.
        """
        value = self._config.append_system_prompt
        if not value or len(value.encode()) <= _MAX_INLINE_APPEND_BYTES:
            return None
        directory = docker_prompt_tmp_dir() if self._config.docker_container else None
        return create_system_prompt_file(value, directory=directory, prefix=_APPEND_PREFIX)

    def _append_arg_path(self, host_path: str | None) -> str | None:
        """Resolve the ``--append-system-prompt-file`` value for the run target.

        In Docker mode the temp file is read through the ``/ductor`` mount, so
        the container-side path is passed to the CLI instead of the host path.
        """
        if host_path is None or not self._config.docker_container:
            return host_path
        container_path = host_path_to_container(host_path)
        if container_path is None:
            msg = f"append-system-prompt temp file is outside the Docker mount: {host_path}"
            raise RuntimeError(msg)
        return container_path


async def _claude_line_handler(line: str) -> AsyncGenerator[StreamEvent, None]:
    """Parse a single Claude stream-json line into stream events."""
    for event in parse_stream_line(line):
        yield event


def _log_cmd(cmd: list[str], *, streaming: bool = False) -> None:
    """Log the Claude CLI command with redacted, truncated long values."""
    kind = "stream cmd" if streaming else "cmd"
    logger.info("CLI %s: %s", kind, format_cli_cmd(cmd))


def _parse_response(stdout: bytes, stderr: bytes, returncode: int | None) -> CLIResponse:
    """Parse CLI subprocess output into a CLIResponse."""
    stderr_text = stderr.decode(errors="replace")[:2000] if stderr else ""
    if stderr_text:
        logger.warning("CLI stderr: %s", stderr_text[:500])

    raw = stdout.decode().strip()
    if not raw:
        logger.error("CLI returned empty output (exit=%s)", returncode)
        return CLIResponse(
            result=stderr_text.strip(),
            is_error=True,
            returncode=returncode,
            stderr=stderr_text,
        )

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
