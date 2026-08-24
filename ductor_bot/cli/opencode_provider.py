"""Async wrapper around the opencode CLI (``opencode``).

opencode headless surface (``opencode run``):

* ``run <message>`` non-interactive single-shot
* ``--format json`` raw NDJSON events on stdout
* ``--session <id>`` / ``--continue`` session continuity
* ``--model <provider>/<model>`` model selection
* ``--auto`` auto-approve permissions that are not explicitly denied
* long prompts are piped through stdin (``opencode run`` reads stdin when
  stdout is not a TTY); there is no ``--prompt-file``-style flag

opencode has no explicit stream ``end`` marker: the process exits when the
session goes idle, so both paths synthesize the final result from the
accumulated text parts.

OpenCode runs through the configured Docker sandbox when enabled. The sandbox
image includes the CLI, while the Docker manager mounts OpenCode's XDG config
and data directories so provider credentials and session continuity are
available inside the container.
"""

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
from ductor_bot.cli.opencode_events import (
    extract_opencode_session_id,
    parse_opencode_json,
    parse_opencode_stream_line,
)
from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent, StreamEvent
from ductor_bot.cli.types import CLIResponse

if TYPE_CHECKING:
    from ductor_bot.cli.timeout_controller import TimeoutController

logger = logging.getLogger(__name__)

# OpenCode argv safety: large prompts go through stdin instead of argv
# (Windows command lines are limited to ~8 KiB; macOS/Linux ARG_MAX is larger).
_PROMPT_ARGV_SOFT_LIMIT = 24_000


class OpencodeCLI(BaseCLI):
    """Async wrapper around the opencode CLI."""

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = self._find_cli()
        logger.info("OpenCode CLI wrapper: cwd=%s, model=%s", self._working_dir, config.model)

    @staticmethod
    def _find_cli() -> str:
        path = which("opencode")
        if not path:
            msg = (
                "opencode CLI not found on PATH. "
                "Install via: curl -fsSL https://opencode.ai/install | bash"
            )
            raise FileNotFoundError(msg)
        return path

    def _build_command(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        *,
        stdin_prompt: bool = False,
    ) -> list[str]:
        cfg = self._config
        cmd = [self._cli, "run"]

        # Session continuity.
        if resume_session:
            cmd += ["--session", resume_session]
        elif continue_session:
            cmd.append("--continue")

        add_cli_opt(cmd, "--model", cfg.model)

        # Auto-approve tools when bypassing permissions; otherwise opencode
        # auto-rejects permission requests (read-only-ish behavior).
        if cfg.permission_mode == "bypassPermissions":
            cmd.append("--auto")

        cmd += ["--format", "json"]

        if cfg.cli_parameters:
            cmd.extend(cfg.cli_parameters)

        # Prompt: argv positional, or stdin when it would overflow argv limits.
        if not stdin_prompt:
            cmd.append(prompt)

        return cmd

    def _use_stdin_prompt(self, prompt: str) -> bool:
        return _IS_WINDOWS or len(prompt) > _PROMPT_ARGV_SOFT_LIMIT

    def _resolve_exec(
        self,
        cmd: list[str],
        *,
        stdin_prompt: bool,
    ) -> tuple[list[str], str | None]:
        """Wrap OpenCode in Docker when configured, including stdin support."""
        # ``which()`` may resolve a host-side installer path such as
        # ``~/.opencode/bin/opencode``. The sandbox image installs the CLI
        # globally, so use its PATH name rather than leaking the host path
        # into ``docker exec``.
        if self._config.docker_container:
            cmd = ["opencode", *cmd[1:]]
        return docker_wrap(cmd, self._config, interactive=stdin_prompt or _IS_WINDOWS)

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> CLIResponse:
        """Send a prompt and return the final result."""
        stdin_prompt = self._use_stdin_prompt(prompt)
        cmd = self._build_command(
            prompt,
            resume_session,
            continue_session,
            stdin_prompt=stdin_prompt,
        )
        exec_cmd, use_cwd = self._resolve_exec(cmd, stdin_prompt=stdin_prompt)
        _log_cmd(exec_cmd, streaming=False)
        return await run_oneshot_subprocess(
            config=self._config,
            spec=SubprocessSpec(
                exec_cmd,
                use_cwd,
                prompt,
                timeout_seconds,
                timeout_controller,
                stdin_text=prompt if stdin_prompt else None,
            ),
            parse_output=_parse_response,
            provider_label="OpenCode",
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
        stdin_prompt = self._use_stdin_prompt(prompt)
        cmd = self._build_command(
            prompt,
            resume_session,
            continue_session,
            stdin_prompt=stdin_prompt,
        )
        exec_cmd, use_cwd = self._resolve_exec(cmd, stdin_prompt=stdin_prompt)
        _log_cmd(exec_cmd, streaming=True)

        accumulated: list[str] = []
        saw_result = False
        session_ids: list[str] = []

        async def line_handler(line: str) -> AsyncGenerator[StreamEvent, None]:
            if not session_ids:
                session_id = extract_opencode_session_id(line)
                if session_id:
                    session_ids.append(session_id)
            for event in parse_opencode_stream_line(line):
                yield event

        async for event in run_streaming_subprocess(
            config=self._config,
            spec=SubprocessSpec(
                exec_cmd,
                use_cwd,
                prompt,
                timeout_seconds,
                timeout_controller,
                stdin_text=prompt if stdin_prompt else None,
            ),
            line_handler=line_handler,
            provider_label="OpenCode",
        ):
            out, is_result = _finalize_stream_event(event, accumulated, session_ids)
            saw_result = saw_result or is_result
            yield out

        if not saw_result and accumulated:
            yield ResultEvent(
                type="result",
                result="".join(accumulated),
                is_error=False,
                session_id=session_ids[0] if session_ids else None,
            )


def _finalize_stream_event(
    event: StreamEvent,
    accumulated: list[str],
    session_ids: list[str],
) -> tuple[StreamEvent, bool]:
    """Normalize one streamed event: accumulate text and backfill final fields."""
    if isinstance(event, AssistantTextDelta) and event.text:
        accumulated.append(event.text)
    if not isinstance(event, ResultEvent):
        return event, False
    out = event
    # Error events often carry no accumulated text; fill from deltas.
    if not event.result and accumulated:
        out = event.model_copy(update={"result": "".join(accumulated)})
    if not out.session_id and session_ids:
        out = out.model_copy(update={"session_id": session_ids[0]})
    return out, True


def _log_cmd(cmd: list[str], *, streaming: bool) -> None:
    """Log the opencode CLI command with truncated long values (no redaction)."""
    kind = "stream cmd" if streaming else "cmd"
    logger.info("OpenCode %s: %s", kind, format_cli_cmd(cmd, redact=False, opt_prefix="-"))


def _parse_response(stdout: bytes, stderr: bytes, returncode: int | None) -> CLIResponse:
    """Parse opencode NDJSON stdout into a CLIResponse."""
    stderr_text = stderr.decode(errors="replace")[:2000] if stderr else ""
    if stderr_text:
        logger.warning("OpenCode stderr: %s", stderr_text[:500])

    raw = stdout.decode().strip()
    if not raw:
        logger.error("OpenCode returned empty output (exit=%s)", returncode)
        return CLIResponse(
            result=stderr_text.strip(),
            is_error=True,
            returncode=returncode,
            stderr=stderr_text,
        )

    text, session_id, usage, model_usage, num_turns, is_error, total_cost = parse_opencode_json(raw)
    if returncode not in (None, 0):
        is_error = True

    response = CLIResponse(
        session_id=session_id,
        result=text,
        is_error=is_error,
        returncode=returncode,
        stderr=stderr_text,
        num_turns=num_turns,
        usage=usage,
        model_usage=model_usage,
        total_cost_usd=total_cost,
    )

    if response.is_error:
        logger.error("OpenCode error: %s", (response.result or stderr_text)[:200])
    else:
        logger.info(
            "OpenCode done session=%s turns=%s cost=$%.4f tokens=%d",
            (response.session_id or "?")[:8],
            response.num_turns,
            response.total_cost_usd or 0,
            response.total_tokens,
        )
    return response
