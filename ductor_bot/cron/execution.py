"""Cron job CLI command building and output parsing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which

from ductor_bot.cli.codex_events import parse_codex_jsonl
from ductor_bot.cli.gemini_events import parse_gemini_json
from ductor_bot.cli.gemini_utils import find_gemini_cli
from ductor_bot.cli.grok_events import parse_grok_json
from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OneShotCommand:
    """Command + optional stdin payload for one-shot execution."""

    cmd: list[str] = field(default_factory=list)
    stdin_input: bytes | None = None
    env_overrides: dict[str, str] = field(default_factory=dict)
    cleanup_paths: list[Path] = field(default_factory=list)


def build_cmd(exec_config: TaskExecutionConfig, prompt: str) -> OneShotCommand | None:
    """Build a CLI command for one-shot cron execution."""
    builder = _CMD_BUILDERS.get(exec_config.provider, _build_claude_cmd)
    return builder(exec_config, prompt)


def enrich_instruction(instruction: str, task_folder: str) -> str:
    """Append memory file instructions to the agent instruction."""
    memory_file = f"{task_folder}_MEMORY.md"
    return (
        f"{instruction}\n\n"
        f"IMPORTANT:\n"
        f"- Read the {memory_file} file (it contains important information!)\n"
        f"- When finished, update {memory_file} with DATE + TIME and what you have done.\n"
        "- The final answer is delivered to Telegram automatically by ductor.\n"
        "- Return only the user-facing result text.\n"
        "- Do not include transport/debug/tool confirmations "
        '(for example: "Message sent successfully").'
    )


def parse_claude_result(stdout: bytes) -> str:
    """Extract result text from Claude CLI JSON output."""
    if not stdout:
        return ""
    raw = stdout.decode(errors="replace").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        return str(data.get("result", ""))
    except json.JSONDecodeError:
        return raw[:2000]


def parse_gemini_result(stdout: bytes) -> str:
    """Extract result text from Gemini CLI JSON output."""
    if not stdout:
        return ""
    raw = stdout.decode(errors="replace").strip()
    if not raw:
        return ""
    return parse_gemini_json(raw) or raw[:2000]


def parse_codex_result(stdout: bytes) -> str:
    """Extract result text from Codex CLI JSONL output."""
    if not stdout:
        return ""
    raw = stdout.decode(errors="replace").strip()
    if not raw:
        return ""
    result_text, thread_id, usage = parse_codex_jsonl(raw)
    # If the JSONL was successfully parsed (thread_id or usage present),
    # an empty result genuinely means no output — don't leak raw events.
    if result_text:
        return result_text
    if thread_id is not None or usage is not None:
        return ""
    return raw[:2000]


def parse_grok_result(stdout: bytes) -> str:
    """Extract result text from Grok Build CLI JSON output."""
    if not stdout:
        return ""
    raw = stdout.decode(errors="replace").strip()
    if not raw:
        return ""
    text, _session_id, _usage, _model_usage, _turns, _is_error, _cost = parse_grok_json(raw)
    if text:
        return text
    return raw[:2000]


def parse_result(provider: str, stdout: bytes) -> str:
    """Extract result text from provider-specific CLI output."""
    parser = _RESULT_PARSERS.get(provider, parse_claude_result)
    return parser(stdout)


# -- Private builders --


def _build_claude_cmd(exec_config: TaskExecutionConfig, prompt: str) -> OneShotCommand | None:
    """Build a Claude CLI command for one-shot cron execution."""
    cli = which("claude")
    if not cli:
        return None
    cmd = [
        cli,
        "-p",
        "--output-format",
        "json",
        "--model",
        exec_config.model,
        "--permission-mode",
        exec_config.permission_mode,
        "--no-session-persistence",
    ]
    # Add reasoning effort, mirroring the -p path's --effort gate ("default" = CLI default).
    if exec_config.reasoning_effort and exec_config.reasoning_effort != "default":
        cmd += ["--effort", exec_config.reasoning_effort]
    # Add extra CLI parameters
    cmd.extend(exec_config.cli_parameters)
    cmd += ["--", prompt]
    return OneShotCommand(cmd=cmd)


def _build_gemini_cmd(exec_config: TaskExecutionConfig, prompt: str) -> OneShotCommand | None:
    """Build a Gemini CLI command for one-shot cron execution.

    Uses hybrid mode: ``-p ""`` forces headless mode (bypassing the TTY check
    that causes exit-42 on Windows), while the actual prompt is fed via stdin.
    """
    try:
        cli = find_gemini_cli()
    except FileNotFoundError:
        return None
    cmd = [cli, "-p", "", "--output-format", "json", "--include-directories", "."]

    if exec_config.model:
        cmd += ["--model", exec_config.model]
    if exec_config.permission_mode == "bypassPermissions":
        cmd += ["--approval-mode", "yolo"]

    cmd.extend(exec_config.cli_parameters)
    return OneShotCommand(cmd=cmd, stdin_input=prompt.encode())


def _build_codex_cmd(exec_config: TaskExecutionConfig, prompt: str) -> OneShotCommand | None:
    """Build a Codex CLI command for one-shot cron execution."""
    cli = which("codex")
    if not cli:
        return None
    cmd = [cli, "exec", "--json", "--color", "never", "--skip-git-repo-check"]

    # Sandbox flags based on permission_mode
    if exec_config.permission_mode == "bypassPermissions":
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.append("--full-auto")

    cmd += ["--model", exec_config.model]

    # Add reasoning effort (if not default)
    if exec_config.reasoning_effort and exec_config.reasoning_effort != "medium":
        cmd += ["-c", f"model_reasoning_effort={exec_config.reasoning_effort}"]

    # Add extra CLI parameters
    cmd.extend(exec_config.cli_parameters)

    cmd += ["--", prompt]
    return OneShotCommand(cmd=cmd)


# Match GrokCLI: long prompts go through --prompt-file to avoid ARG_MAX.
_GROK_PROMPT_ARGV_SOFT_LIMIT = 24_000


def _build_grok_cmd(exec_config: TaskExecutionConfig, prompt: str) -> OneShotCommand | None:
    """Build a Grok Build CLI command for one-shot cron execution."""
    cli = which("grok")
    if not cli:
        return None
    cmd = [cli, "--output-format", "json"]
    if exec_config.model:
        cmd += ["--model", exec_config.model]
    if exec_config.permission_mode:
        cmd += ["--permission-mode", exec_config.permission_mode]
    if exec_config.permission_mode == "bypassPermissions":
        cmd.append("--always-approve")
    if exec_config.reasoning_effort and exec_config.reasoning_effort != "default":
        cmd += ["--reasoning-effort", exec_config.reasoning_effort]
    cmd.extend(exec_config.cli_parameters)

    cleanup: list[Path] = []
    if len(prompt) > _GROK_PROMPT_ARGV_SOFT_LIMIT:
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w",
            encoding="utf-8",
            prefix="ductor-grok-cron-prompt-",
            suffix=".txt",
            delete=False,
        )
        with handle:
            handle.write(prompt)
            prompt_path = Path(handle.name)
        cleanup.append(prompt_path)
        cmd += ["--prompt-file", str(prompt_path)]
    else:
        cmd += ["-p", prompt]
    return OneShotCommand(cmd=cmd, cleanup_paths=cleanup)


_CmdBuilder = Callable[[TaskExecutionConfig, str], OneShotCommand | None]
_ResultParser = Callable[[bytes], str]

_CMD_BUILDERS: dict[str, _CmdBuilder] = {
    "claude": _build_claude_cmd,
    "gemini": _build_gemini_cmd,
    "codex": _build_codex_cmd,
    "grok": _build_grok_cmd,
}

_RESULT_PARSERS: dict[str, _ResultParser] = {
    "claude": parse_claude_result,
    "gemini": parse_gemini_result,
    "codex": parse_codex_result,
    "grok": parse_grok_result,
}


@dataclass(slots=True)
class OneShotExecutionResult:
    """Normalized outcome for a one-shot provider subprocess run."""

    status: str
    result_text: str
    stdout: bytes
    stderr: bytes
    returncode: int | None
    timed_out: bool


def _force_kill(proc: asyncio.subprocess.Process) -> None:
    """Force-kill a subprocess and any descendants."""
    force_kill_process_tree(proc.pid)


async def execute_one_shot(
    one_shot: OneShotCommand,
    *,
    cwd: Path,
    provider: str,
    timeout_seconds: float,
    timeout_label: str,
) -> OneShotExecutionResult:
    """Run one provider CLI command with timeout and normalized status/result."""
    stdin_input = one_shot.stdin_input
    env = {**os.environ, **one_shot.env_overrides} if one_shot.env_overrides else None
    try:
        proc = await asyncio.create_subprocess_exec(
            *one_shot.cmd,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE
            if stdin_input is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATION_FLAGS,
        )

        timed_out = False
        try:
            async with asyncio.timeout(timeout_seconds):
                stdout, stderr = await proc.communicate(input=stdin_input)
        except TimeoutError:
            timed_out = True
            _force_kill(proc)
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            _force_kill(proc)
            await proc.wait()
            raise

        if timed_out:
            return OneShotExecutionResult(
                status="error:timeout",
                result_text=f"[{timeout_label} timed out after {timeout_seconds:.0f}s]",
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                timed_out=True,
            )

        returncode = proc.returncode
        status = "success" if returncode == 0 else f"error:exit_{returncode}"
        return OneShotExecutionResult(
            status=status,
            result_text=parse_result(provider, stdout),
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            timed_out=False,
        )
    finally:
        for path in one_shot.cleanup_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Failed to remove one-shot temp path %s", path)
