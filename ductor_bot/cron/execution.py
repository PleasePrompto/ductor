"""Cron job CLI command building and output parsing."""

from __future__ import annotations

import json
import logging
from shutil import which
from typing import TYPE_CHECKING

from ductor_bot.cli.codex_events import parse_codex_jsonl

if TYPE_CHECKING:
    from ductor_bot.cli.param_resolver import TaskExecutionConfig

logger = logging.getLogger(__name__)


def build_cmd(exec_config: TaskExecutionConfig, prompt: str) -> list[str] | None:
    """Build a CLI command for one-shot cron execution."""
    if exec_config.provider == "codex":
        return _build_codex_cmd(exec_config, prompt)
    return _build_claude_cmd(exec_config, prompt)


def enrich_instruction(instruction: str, task_folder: str) -> str:
    """Append memory file instructions to the agent instruction."""
    memory_file = f"{task_folder}_MEMORY.md"
    return (
        f"{instruction}\n\n"
        f"IMPORTANT:\n"
        f"- Read the {memory_file} file (it contains important information!)\n"
        f"- When finished, update {memory_file} with DATE + TIME and what you have done."
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


def parse_codex_result(stdout: bytes) -> str:
    """Extract result text from Codex CLI JSONL output."""
    if not stdout:
        return ""
    raw = stdout.decode(errors="replace").strip()
    if not raw:
        return ""
    result_text, _thread_id, _usage = parse_codex_jsonl(raw)
    return result_text or raw[:2000]


def indent(text: str, prefix: str) -> str:
    """Indent every line of *text* with *prefix*."""
    return "\n".join(prefix + line for line in text.splitlines())


# -- Private builders --


def _build_claude_cmd(exec_config: TaskExecutionConfig, prompt: str) -> list[str] | None:
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
    # Add extra CLI parameters
    cmd.extend(exec_config.cli_parameters)
    cmd += ["--", prompt]
    return cmd


def _build_codex_cmd(exec_config: TaskExecutionConfig, prompt: str) -> list[str] | None:
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
    return cmd
