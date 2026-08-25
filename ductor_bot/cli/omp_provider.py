"""Async wrapper around the Oh My Pi CLI (``omp``)."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING

from ductor_bot.cli.base import (
    _IS_WINDOWS,
    BaseCLI,
    CLIConfig,
    add_cli_opt,
    docker_wrap,
    format_cli_cmd,
)
from ductor_bot.cli.executor import SubprocessSpec, run_oneshot_subprocess, run_streaming_subprocess
from ductor_bot.cli.omp_events import parse_omp_json, parse_omp_stream_line
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
)
from ductor_bot.cli.types import CLIResponse

if TYPE_CHECKING:
    from ductor_bot.cli.timeout_controller import TimeoutController

logger = logging.getLogger(__name__)


class OmpCLI(BaseCLI):
    """Async wrapper around the Oh My Pi CLI (``omp``)."""

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = "omp" if config.docker_container else self._find_cli()
        logger.info("Omp CLI wrapper: cwd=%s, model=%s", self._working_dir, config.model)

    @staticmethod
    def _find_cli() -> str:
        path = which("omp")
        if not path:
            msg = (
                "omp CLI not found on PATH. "
                "Install via: https://github.com/oh-my-pi/pi or `npm i -g @oh-my-pi/cli`"
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
        cmd = [self._cli, "-p", "--mode", "json"]

        if cfg.model:
            cmd += ["--model", cfg.model]
        if cfg.reasoning_effort and cfg.reasoning_effort not in ("default", "medium"):
            cmd += ["--thinking", cfg.reasoning_effort]
        add_cli_opt(cmd, "--system-prompt", cfg.system_prompt)
        add_cli_opt(cmd, "--append-system-prompt", cfg.append_system_prompt)

        if cfg.permission_mode == "bypassPermissions":
            cmd.append("--auto-approve")

        if resume_session:
            cmd += ["--resume", resume_session]
        elif continue_session:
            cmd.append("--continue")

        if cfg.cli_parameters:
            cmd.extend(cfg.cli_parameters)

        # Omp accepts the prompt as a trailing positional or via stdin.
        # Use positional to match other providers; stdin fallback is handled
        # by the executor on Windows.
        if not _IS_WINDOWS:
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
        cmd = self._build_command(prompt, resume_session, continue_session)
        exec_cmd, use_cwd = docker_wrap(cmd, self._config, interactive=_IS_WINDOWS)
        _log_cmd(exec_cmd)
        stdin_text = prompt if _IS_WINDOWS else None
        return await run_oneshot_subprocess(
            config=self._config,
            spec=SubprocessSpec(
                exec_cmd,
                use_cwd,
                prompt,
                timeout_seconds,
                timeout_controller,
                stdin_text=stdin_text,
            ),
            parse_output=_parse_response,
            provider_label="Omp",
        )

    async def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Send a prompt and yield stream events as they arrive."""
        cmd = self._build_command(prompt, resume_session, continue_session)
        exec_cmd, use_cwd = docker_wrap(cmd, self._config, interactive=_IS_WINDOWS)
        _log_cmd(exec_cmd, streaming=True)
        stdin_text = prompt if _IS_WINDOWS else None
        accumulated: list[str] = []
        saw_result = False

        async for event in run_streaming_subprocess(
            config=self._config,
            spec=SubprocessSpec(
                exec_cmd,
                use_cwd,
                prompt,
                timeout_seconds,
                timeout_controller,
                stdin_text=stdin_text,
            ),
            line_handler=_omp_line_handler,
            provider_label="Omp",
        ):
            if isinstance(event, AssistantTextDelta) and event.text:
                accumulated.append(event.text)
            out: StreamEvent = event
            if isinstance(event, ResultEvent):
                saw_result = True
                if not event.result and accumulated:
                    out = event.model_copy(update={"result": "".join(accumulated)})
            yield out

        if not saw_result and accumulated:
            yield ResultEvent(type="result", result="".join(accumulated), is_error=False)


async def _omp_line_handler(line: str) -> AsyncGenerator[StreamEvent, None]:
    """Parse a single Omp NDJSON line into stream events."""
    for event in parse_omp_stream_line(line):
        yield event


def _log_cmd(cmd: list[str], *, streaming: bool = False) -> None:
    """Log the Omp CLI command with truncated long values."""
    kind = "stream cmd" if streaming else "cmd"
    logger.info("Omp %s: %s", kind, format_cli_cmd(cmd, redact=False))


def _parse_response(stdout: bytes, stderr: bytes, returncode: int | None) -> CLIResponse:
    """Parse Omp oneshot output into a CLIResponse."""
    raw = stdout.decode(errors="replace")
    err_text = stderr.decode(errors="replace")[:2000]

    if not raw.strip():
        detail = err_text.strip() or "(no output)"
        return CLIResponse(
            result=detail,
            is_error=True,
            returncode=returncode,
            stderr=err_text,
        )

    try:
        text, session_id, usage, model_usage, num_turns, is_error, total_cost = parse_omp_json(raw)
    except Exception:
        logger.exception("Omp: failed to parse JSON envelope")
        return CLIResponse(
            result=raw.strip()[:8000],
            is_error=True,
            returncode=returncode,
            stderr=err_text,
        )

    # Non-zero exit overrides payload error state
    if returncode not in (None, 0):
        is_error = True
        if not text:
            text = err_text.strip() or raw.strip()[:2000]

    # Surface stderr details when payload had no text
    if is_error and not text and err_text.strip():
        text = err_text.strip()

    return CLIResponse(
        session_id=session_id,
        result=text,
        is_error=is_error,
        returncode=returncode,
        stderr=err_text,
        usage=usage,
        model_usage=model_usage,
        num_turns=num_turns,
        total_cost_usd=total_cost,
    )


# Backwards-compat: some call sites imported helpers via executor path.
__all__ = ["OmpCLI"]
