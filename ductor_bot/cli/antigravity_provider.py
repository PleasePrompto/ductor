"""Async wrapper around the Antigravity CLI (agy)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.antigravity_events import parse_antigravity_json
from ductor_bot.cli.antigravity_runtime import antigravity_process_env
from ductor_bot.cli.base import BaseCLI, CLIConfig
from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.cli.process_registry import ProcessRegistry, TrackedProcess
from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent, StreamEvent
from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import ANTIGRAVITY_MODELS
from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

# Reuse the cross-platform directory-link helper (symlink with Windows junction
# fallback) to work around agy's hidden-dotted-workspace bug; see
# _safe_agy_workspace below.
from ductor_bot.workspace.skill_sync import _create_dir_link

if TYPE_CHECKING:
    from ductor_bot.cli.timeout_controller import TimeoutController

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300.0  # 5 minutes, matches agy --print-timeout default


class AntigravityCLI(BaseCLI):
    """Async wrapper around the Antigravity CLI (agy).

    agy has no headless streaming protocol: ``--print`` is one-shot and
    ``--prompt-interactive`` is a bubbletea TUI that needs a real ``/dev/tty``
    a subprocess does not have. Both :meth:`send` and :meth:`send_streaming`
    drive the same ``--print`` command.

    ``agy --print`` also silently drops its stdout when stdout is not a TTY
    (pipe/subprocess/redirect) -- upstream bug
    ``google-antigravity/antigravity-cli#76``. The answer is therefore read
    back from agy's own per-conversation transcript
    (``<home>/.gemini/antigravity-cli/brain/<conv-id>/.system_generated/logs/transcript.jsonl``),
    taking the final ``source=MODEL, type=PLANNER_RESPONSE, status=DONE``
    entry's ``content`` -- the clean answer without the intermediate tool-call
    narration. stdout is used only as a fallback.

    agy flags reference:
      --print / -p <prompt>   Non-interactive single-shot
      --continue / -c         Continue most recent conversation
      --conversation <id>     Resume a specific conversation
      --dangerously-skip-permissions  Auto-approve all tools
      --print-timeout <dur>   Timeout for --print mode (default 5m)
      --model <id>            Select a model (see ``agy models``)
      --add-dir <dir>         Add workspace directory
      --log-file <path>       Override log file
    """

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = "agy"
        self._agy_workspace_cache: Path | None = None
        logger.info("AntigravityCLI: cwd=%s model=%s", self._working_dir, config.model)

    @property
    def _agy_workspace(self) -> Path:
        """Path agy accepts as a workspace; resolved lazily on first use.

        agy rejects any workspace whose path has a dot-prefixed ancestor (e.g.
        ~/.ductor/workspace) and falls back to its scratch sandbox, so the
        workspace is exposed through a non-dotted symlink. That symlink is
        created only here -- when agy is actually about to run -- so it never
        appears for users who only use claude/codex/gemini.
        """
        if self._agy_workspace_cache is None:
            self._agy_workspace_cache = _safe_agy_workspace(self._working_dir)
        return self._agy_workspace_cache

    def _build_command(
        self,
        prompt: str,
        *,
        resume_session: str | None = None,
        continue_session: bool = False,
    ) -> list[str]:
        """Build the full ``agy`` command.

        ``--print`` is a string flag that consumes the *next* token as the
        prompt, so it must come last with the prompt immediately after it.
        Otherwise ``agy --print --model X <prompt>`` makes agy treat
        ``--model`` as the prompt and silently drops both the real prompt and
        the requested model (falling back to its default).
        """
        cmd = [self._cli]

        # Ground agy in ductor's per-agent workspace so its tools operate there
        # instead of falling back to agy's own scratch sandbox
        # (~/.gemini/antigravity-cli/scratch). Because agy keys conversations by
        # cwd, this also keeps main agent, sub-agents and topics that use
        # distinct working dirs isolated -- and matches the cwd the transcript
        # reader resolves the answer from.
        cmd += ["--add-dir", str(self._agy_workspace)]

        if self._config.model and self._config.model not in ANTIGRAVITY_MODELS:
            cmd += ["--model", self._config.model]

        # Session resume / continue
        if resume_session:
            cmd += ["--conversation", resume_session]
        elif continue_session:
            cmd += ["--continue"]

        # Auto-approve when bypass mode is set
        if self._config.permission_mode == "bypassPermissions":
            cmd += ["--dangerously-skip-permissions"]

        cmd.extend(self._config.cli_parameters)

        # --print and its prompt value MUST be last and adjacent (see docstring).
        cmd += ["--print", prompt]
        return cmd

    def _host_command(self, cmd: list[str]) -> tuple[list[str], str]:
        """Return a host-execution command.

        Antigravity is a host CLI. The standard Docker sandbox image does not
        include ``agy`` or its user auth state, so running it through
        ``docker exec`` produces an OCI "agy not found" error.
        """
        if self._config.docker_container:
            logger.info("Antigravity runs on host; ignoring Docker container for agy")
        return cmd, str(self._agy_workspace)

    # -- Process tracking -----------------------------------------------------

    def _track_process(
        self,
        process: asyncio.subprocess.Process,
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

    # -- Non-streaming --------------------------------------------------------

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> CLIResponse:
        """Send a prompt via ``agy --print`` and return the full response."""
        effective_timeout = timeout_seconds or _DEFAULT_TIMEOUT
        cmd = self._build_command(
            prompt,
            resume_session=resume_session,
            continue_session=continue_session,
        )

        cmd, cwd = self._host_command(cmd)
        safe_cmd = _safe_command_for_logging(cmd)
        logger.debug("Antigravity send: %s", safe_cmd)

        env = antigravity_process_env(build_subprocess_env(self._config))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            creationflags=_CREATION_FLAGS,
        )

        reg, tracked = self._track_process(proc)

        try:
            timed_out = False
            try:
                communicate_coro = proc.communicate()
                if timeout_controller is not None:
                    stdout_bytes, stderr_bytes = await timeout_controller.run_with_timeout(
                        communicate_coro,
                    )
                else:
                    async with asyncio.timeout(effective_timeout):
                        stdout_bytes, stderr_bytes = await communicate_coro
            except TimeoutError:
                timed_out = True
                logger.warning("Antigravity send timed out")
                force_kill_process_tree(proc.pid)
                stdout_bytes, stderr_bytes = await proc.communicate()
                return CLIResponse(
                    result="Timeout",
                    is_error=True,
                    timed_out=True,
                    returncode=proc.returncode,
                    stderr=stderr_bytes.decode(errors="replace")[:2000] if stderr_bytes else "",
                )
        finally:
            self._untrack_process(reg, tracked)
            if not timed_out and proc.returncode is None:
                force_kill_process_tree(proc.pid)

        stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""

        # agy --print silently drops stdout in non-TTY subprocesses (upstream
        # bug antigravity-cli#76), so prefer agy's own transcript file, which
        # also yields the clean final answer without tool-call narration.
        # stdout is the fallback for environments/versions where it works.
        transcript_answer = _read_transcript_answer(self._agy_workspace, env)
        if transcript_answer is not None:
            logger.debug("Antigravity answer read from transcript")
            result_text = transcript_answer
        else:
            result_text = parse_antigravity_json(stdout)
        is_error = proc.returncode not in (None, 0)

        return CLIResponse(
            result=result_text,
            is_error=is_error,
            returncode=proc.returncode,
            stderr=stderr,
        )

    # -- Streaming ------------------------------------------------------------

    async def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream a response from agy.

        agy exposes no incremental stream, so this runs the one-shot
        ``--print`` path and emits the answer as a single text delta followed
        by the final result event. This keeps the streaming contract intact
        for the orchestrator while matching what the CLI can actually do.
        """
        response = await self.send(
            prompt,
            resume_session=resume_session,
            continue_session=continue_session,
            timeout_seconds=timeout_seconds,
            timeout_controller=timeout_controller,
        )

        if response.result:
            yield AssistantTextDelta(type="assistant", text=response.result)

        yield ResultEvent(
            type="result",
            result=response.result,
            is_error=response.is_error,
            returncode=response.returncode,
            session_id=response.session_id,
        )


# -- Module-level helpers -----------------------------------------------------


def _safe_command_for_logging(cmd: list[str]) -> list[str]:
    """Return a command safe for debug logs."""
    safe = [part if len(part) <= 80 else part[:80] + "..." for part in cmd]
    if "--print" in cmd and safe:
        safe[-1] = "<prompt>"
    return safe


# -- Workspace path (workaround for antigravity-cli#20) ------------------------
#
# agy rejects any workspace folder whose path contains a dot-prefixed ancestor
# ("... is hidden: ignore uri") and silently falls back to its scratch sandbox,
# so ductor's ~/.ductor/workspace is never accepted. agy checks the literal path
# it is given, not the resolved target, so a non-dotted directory symlink to the
# real workspace works around it.
# See https://github.com/google-antigravity/antigravity-cli/issues/20


def _safe_agy_workspace(working_dir: Path) -> Path:
    """Return a path agy will accept as a workspace for *working_dir*.

    If *working_dir* has a dot-prefixed ancestor, expose it via a non-dotted
    sibling symlink and return the symlinked path. Falls back to *working_dir*
    if there is no such ancestor or the symlink cannot be created (agy then uses
    its scratch sandbox -- degraded, not broken).
    """
    if "/." not in working_dir.as_posix():
        return working_dir

    parts = working_dir.parts
    for index, segment in enumerate(parts):
        if segment.startswith(".") and segment not in (".", ".."):
            dot_ancestor = Path(*parts[: index + 1])
            link = dot_ancestor.with_name(segment[1:])  # strip the leading dot
            remainder = Path(*parts[index + 1 :]) if index + 1 < len(parts) else Path()
            if _ensure_agy_link(link, dot_ancestor):
                return link / remainder
            return working_dir
    return working_dir


def _ensure_agy_link(link: Path, target: Path) -> bool:
    """Idempotently point the non-dotted *link* at *target*; return success."""
    try:
        if link.is_symlink():
            if link.resolve() == target.resolve():
                return True
            link.unlink()
        elif link.exists():
            # A real directory occupies the path -- never clobber it.
            logger.warning("Antigravity: %s exists and is not a symlink; using scratch", link)
            return False
        link.parent.mkdir(parents=True, exist_ok=True)
        _create_dir_link(link, target)
    except OSError as exc:
        logger.warning("Antigravity: could not link %s -> %s (%s)", link, target, exc)
        return False
    return link.exists()


# -- Transcript reading (workaround for antigravity-cli#76) --------------------
#
# ``agy --print`` completes the model round-trip but writes nothing to stdout
# when stdout is not a TTY (pipe/subprocess). It persists the full turn to a
# per-conversation JSONL transcript instead, so the answer is read from there.
# See https://github.com/google-antigravity/antigravity-cli/issues/76


def _agy_state_root(env: Mapping[str, str] | None = None) -> Path:
    """Locate agy's per-user state dir, cross-platform.

    agy stores conversations under ``<home>/.gemini/antigravity-cli`` where
    ``<home>`` is the user's home directory on every platform -- ``HOME`` on
    Linux/macOS, ``USERPROFILE`` on Windows. It is derived from the same
    environment handed to the agy subprocess so ductor reads exactly where agy
    wrote, falling back to the current user's home.
    """
    source = env if env is not None else os.environ
    home = source.get("USERPROFILE") or source.get("HOME")
    base = Path(home) if home else Path.home()
    return base / ".gemini" / "antigravity-cli"


def _read_transcript_answer(working_dir: Path, env: Mapping[str, str] | None = None) -> str | None:
    """Return agy's final answer for *working_dir* from its transcript, or None.

    The answer is the last ``source=MODEL, type=PLANNER_RESPONSE, status=DONE``
    entry's ``content`` -- already free of the intermediate tool-call steps.
    """
    brain_dir = _resolve_brain_dir(working_dir, env)
    if brain_dir is None:
        return None
    transcript = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
    try:
        raw = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    answer: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(entry, dict)
            and entry.get("source") == "MODEL"
            and entry.get("type") == "PLANNER_RESPONSE"
            and entry.get("status") == "DONE"
        ):
            content = entry.get("content")
            if isinstance(content, str) and content.strip():
                answer = content
    return answer


def _resolve_brain_dir(working_dir: Path, env: Mapping[str, str] | None = None) -> Path | None:
    """Locate the ``brain/<conv-id>`` dir for *working_dir*'s latest turn."""
    root = _agy_state_root(env)
    brain_root = root / "brain"

    conv_id = _conv_id_for_cwd(root, working_dir)
    if conv_id:
        candidate = brain_root / conv_id
        if candidate.is_dir():
            return candidate

    return _newest_brain_dir(brain_root)


def _conv_id_for_cwd(root: Path, working_dir: Path) -> str | None:
    """Map a working directory to its conversation id via agy's cwd cache."""
    mapping_path = root / "cache" / "last_conversations.json"
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(mapping, dict):
        return None
    for key in (str(working_dir), os.path.realpath(working_dir)):
        conv = mapping.get(key)
        if isinstance(conv, str) and conv:
            return conv
    return None


def _newest_brain_dir(brain_root: Path) -> Path | None:
    """Return the conversation dir with the most recently written transcript."""
    try:
        candidates = [entry for entry in brain_root.iterdir() if entry.is_dir()]
    except OSError:
        return None
    best: Path | None = None
    best_mtime = -1.0
    for directory in candidates:
        transcript = directory / ".system_generated" / "logs" / "transcript.jsonl"
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best_mtime = mtime
            best = directory
    return best
