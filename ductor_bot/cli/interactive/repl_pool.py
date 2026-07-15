"""tmux-backed Claude interactive REPL pool."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shlex
import subprocess
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ductor_bot.cli.base import DOCKER_INTERAGENT_HOST
from ductor_bot.cli.ductor_env import build_ductor_env

CommandRunner = Callable[[Sequence[str], str | None], subprocess.CompletedProcess[str]]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]
TokenFactory = Callable[[], str]

DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")
_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


class ReplTimeoutError(TimeoutError):
    """Raised when a REPL turn does not produce its Stop-hook signal in time."""


class ReplSessionBusyError(RuntimeError):
    """Raised when an eviction would kill an in-progress turn."""


class ReplFatalError(RuntimeError):
    """Raised when a REPL turn cannot complete.

    The Claude process exited, or the pane shows a fatal screen (rejected model,
    auth failure); fail fast instead of blocking the full Stop-hook timeout.
    """


@dataclass(frozen=True, slots=True)
class ReplPoolConfig:
    """Configurable paths and pool limits for interactive Claude."""

    agent: str
    working_dir: Path
    claude_home: Path
    signal_dir: Path
    # DUCTOR_* env supply (mode-correct: host paths host-mode, container paths
    # docker-mode). Mirrors what the -p path sets via build_ductor_env.
    ductor_home: Path = Path()
    shared_memory_path: Path = Path()
    interagent_port: int = 8799
    transcribe_command: str = ""
    video_transcribe_command: str = ""
    container_working_dir: Path | None = None
    container_claude_home: Path | None = None
    container_signal_dir: Path | None = None
    docker_container: str = ""
    idle_ttl_seconds: float = 30 * 60
    max_sessions: int = 4
    # Fallback when the request carries no timeout; matches the -p path scale
    # (cli_timeout default 1800) so long agentic turns are not cut off mid-work.
    signal_timeout_seconds: float = 1800
    boot_timeout_seconds: float = 20
    boot_key_delay: float = 0.4
    tmux_bin: str = "tmux"
    claude_bin: str = "claude"
    tmux_columns: int = 200
    tmux_rows: int = 50
    paste_threshold: int = 512
    permission_mode: str = "default"
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    tool_denylist: tuple[str, ...] = ()


@dataclass(slots=True)
class ReplSession:
    """One tmux-backed Claude REPL session."""

    agent: str
    transport: str
    chat: str
    topic: str
    name: str
    model: str
    system_prompt: str
    effort: str = ""
    session_id: str | None = None
    transcript_path: Path | None = None
    last_active: float = 0
    in_progress: bool = False


@dataclass(frozen=True, slots=True)
class _SignalMatch:
    path: Path
    session_id: str


def _default_runner(
    args: Sequence[str], input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def _safe_part(value: object) -> str:
    """Make one tmux-session-safe name component."""
    text = str(value) if value not in (None, "") else "none"
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-.")
    return cleaned or "none"


def session_name(agent: str, transport: object, chat: object, topic: object) -> str:
    """Return the canonical tmux session name for a REPL."""
    return (
        f"ductor-repl-{_safe_part(agent)}-{_safe_part(transport)}"
        f"-{_safe_part(chat)}-{_safe_part(topic)}"
    )


def normalized_screen(text: str) -> str:
    """Normalize TUI text for whitespace-insensitive matching."""
    return re.sub(r"\s+", "", text).lower()


def boot_actions_for_screen(screen: str) -> tuple[str, ...]:
    """Return tmux key actions needed for a Claude boot prompt screen."""
    normalized = normalized_screen(screen)
    if not normalized:
        return ()
    if "bypasspermissions" in normalized or "yes,iaccept" in normalized:
        return ("Down", "Enter")
    trust_markers = ("doyoutrust", "trustthefiles", "trustthisfolder", "trustthisproject")
    if any(marker in normalized for marker in trust_markers):
        return ("Enter",)
    return ()


# Whitespace-removed, lowercased fatal markers (normalized_screen output).
_FATAL_SCREEN_MARKERS: tuple[tuple[str, str], ...] = (
    ("issuewiththeselectedmodel", "model rejected by Claude"),
    ("maynothaveaccess", "model not accessible (geo/plan restriction)"),
    ("invalidapikey", "invalid API key"),
    ("authenticationerror", "authentication error"),
    ("pleaserun/login", "not logged in (run /login)"),
    ("401invalidauthenticationcredentials", "API 401: invalid auth credentials"),
)


def fatal_error_for_screen(screen: str) -> str | None:
    """Return a short reason when the pane shows a fatal, non-recoverable state.

    Used so a turn that can never complete (e.g. a rejected/geo-blocked model)
    aborts immediately instead of waiting out the Stop-hook timeout.
    """
    normalized = normalized_screen(screen)
    if not normalized:
        return None
    for marker, reason in _FATAL_SCREEN_MARKERS:
        if marker in normalized:
            return reason
    return None


def _content_to_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        text = value.get("text")
        if isinstance(text, str):
            return text
        content = value.get("content")
        return _content_to_text(content)
    if isinstance(value, Iterable) and not isinstance(value, bytes):
        return "".join(_content_to_text(item) for item in value)
    return ""


def _event_type(event: Mapping[str, Any]) -> str:
    raw_type = event.get("type")
    if isinstance(raw_type, str):
        return raw_type
    message = event.get("message")
    if isinstance(message, Mapping):
        role = message.get("role")
        if isinstance(role, str):
            return role
    return ""


def _event_text(event: Mapping[str, Any]) -> str:
    message = event.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if content is not None:
            return _content_to_text(content)
    content = event.get("content")
    if content is not None:
        return _content_to_text(content)
    result = event.get("result")
    if result is not None:
        return _content_to_text(result)
    return ""


def iter_transcript_events(path: Path, *, start_offset: int = 0) -> Iterable[dict[str, Any]]:
    """Yield transcript JSONL events from a byte offset."""
    with path.open("r", encoding="utf-8") as handle:
        if start_offset:
            handle.seek(start_offset)
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def _is_tool_result_event(event: Mapping[str, Any]) -> bool:
    """True when a type="user" event only carries tool results, not a prompt.

    Tool results come back as user-role events in the transcript; treating
    them as the next human turn truncates extraction for tool-using turns.
    """
    message = event.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return False
    block_types = {b.get("type") for b in content if isinstance(b, Mapping)}
    return bool(block_types) and block_types <= {"tool_result"}


def extract_reply_after_nonce(path: Path, *, nonce: str, start_offset: int = 0) -> str:
    """Extract assistant text after the user event carrying the reply nonce."""
    sentinel = f"[[reply-token:{nonce}]]"
    found_user = False
    chunks: list[str] = []
    for event in iter_transcript_events(path, start_offset=start_offset):
        event_type = _event_type(event)
        if event_type == "user":
            if _is_tool_result_event(event):
                continue
            if found_user:
                break
            found_user = sentinel in _event_text(event)
            continue
        if not found_user:
            continue
        if event_type == "assistant":
            text = _event_text(event)
            if text:
                chunks.append(text)
            continue
        if event_type == "result":
            break
    return "".join(chunks).strip()


class ReplPool:
    """Manage human-chat-only tmux-backed Claude REPL sessions by (chat, topic)."""

    def __init__(
        self,
        config: ReplPoolConfig,
        *,
        runner: CommandRunner = _default_runner,
        now: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
        token_factory: TokenFactory | None = None,
    ) -> None:
        self._config = config
        self._runner = runner
        self._now = now
        self._sleep = sleep
        self._token_factory = token_factory or (lambda: secrets.token_hex(8))
        self._sessions: OrderedDict[tuple[str, str, str], ReplSession] = OrderedDict()

    def spawn(  # noqa: PLR0913
        self,
        *,
        agent: str,
        transport: str,
        chat: object,
        topic: object,
        model: str,
        system_prompt: str,
        effort: str = "",
        resume_session: str | None = None,
    ) -> ReplSession:
        """Spawn a tmux session running interactive Claude."""
        name = session_name(agent, transport, chat, topic)
        session = ReplSession(
            agent=agent,
            transport=transport,
            chat=str(chat),
            topic=str(topic) if topic is not None else "none",
            name=name,
            model=model,
            effort=effort,
            system_prompt=system_prompt,
            session_id=resume_session,
            last_active=self._now(),
        )
        self._run(
            [
                self._config.tmux_bin,
                "new-session",
                "-d",
                "-s",
                name,
                "-x",
                str(self._config.tmux_columns),
                "-y",
                str(self._config.tmux_rows),
                "-c",
                str(self._container_working_dir()),
                "sh",
                "-lc",
                self._claude_command(
                    model,
                    system_prompt,
                    resume_session,
                    session.transport,
                    session.chat,
                    session.topic,
                    effort,
                ),
            ]
        )
        self._handle_boot_prompts(name)
        return session

    def send(  # noqa: PLR0913
        self,
        *,
        transport: str,
        chat: object,
        topic: object,
        model: str,
        system_prompt: str,
        prompt: str,
        effort: str = "",
        timeout_seconds: float | None = None,
    ) -> tuple[str, str]:
        """Send a one-shot prompt through a pooled REPL and return text/session_id."""
        key = (transport, str(chat), str(topic) if topic is not None else "none")
        session = self._get_or_spawn(key, transport, chat, topic, model, system_prompt, effort)
        if session.in_progress:
            raise ReplSessionBusyError(f"REPL session already has an active turn: {session.name}")

        session.in_progress = True
        try:
            nonce = self._token_factory()
            cursor = self._transcript_size(session)
            # Snapshot the pane *before* sending: on --resume the pane redraws
            # past conversation (which may contain an old model-rejection
            # error), so only a marker that is NEW vs this baseline is a real
            # failure of the current turn.
            baseline_screen = self._capture_pane(session.name)
            self._send_text(session.name, f"{prompt}\n[[reply-token:{nonce}]]")
            try:
                match = self._wait_for_signal(
                    nonce=nonce,
                    session_id=session.session_id,
                    tmux_name=session.name,
                    baseline_screen=baseline_screen,
                    timeout_seconds=timeout_seconds,
                )
            except ReplFatalError:
                # Drop the unhealthy session so the next turn respawns clean.
                self._sessions.pop(key, None)
                self._kill_session(session.name)
                raise
            session.session_id = match.session_id
            transcript_path = self._find_transcript(match.session_id)
            if transcript_path is None:
                raise FileNotFoundError(
                    f"Claude transcript not found for session {match.session_id}"
                )
            session.transcript_path = transcript_path
            text = extract_reply_after_nonce(transcript_path, nonce=nonce, start_offset=cursor)
            if not text and cursor:
                text = extract_reply_after_nonce(transcript_path, nonce=nonce, start_offset=0)
            with contextlib.suppress(OSError):
                match.path.unlink()
            session.last_active = self._now()
            self._sessions.move_to_end(key)
            return text, match.session_id
        finally:
            session.in_progress = False

    def seed_resume_session(  # noqa: PLR0913
        self,
        *,
        transport: str,
        chat: object,
        topic: object,
        model: str,
        system_prompt: str,
        session_id: str | None,
        effort: str = "",
    ) -> None:
        """Seed the pool with a persisted Claude session id for lazy --resume spawn."""
        if not session_id:
            return
        key = (transport, str(chat), str(topic) if topic is not None else "none")
        if key in self._sessions:
            return
        self._sessions[key] = ReplSession(
            agent=self._config.agent,
            transport=transport,
            chat=str(chat),
            topic=str(topic) if topic is not None else "none",
            name=session_name(self._config.agent, transport, chat, topic),
            model=model,
            effort=effort,
            system_prompt=system_prompt,
            session_id=session_id,
            last_active=self._now(),
        )

    def kill(self, *, transport: str, chat: object, topic: object) -> int:
        """Kill the exact tmux REPL session for *(transport, chat, topic)*, if present."""
        key = (transport, str(chat), str(topic) if topic is not None else "none")
        session = self._sessions.pop(key, None)
        tmux_name = (
            session.name
            if session is not None
            else session_name(self._config.agent, transport, chat, topic)
        )
        if not self._session_exists(tmux_name):
            return 0
        self._kill_session(tmux_name)
        return 1

    def startup_sweep(self, agent: str | None = None) -> None:
        """Kill only this agent's stale ductor REPL tmux sessions."""
        self.kill_all(agent)

    def kill_all(self, agent: str | None = None) -> int:
        """Kill all tmux REPL sessions for this agent."""
        prefix = f"ductor-repl-{_safe_part(agent or self._config.agent)}-"
        result = self._call([self._config.tmux_bin, "ls"], None)
        if result.returncode != 0:
            return 0
        killed = 0
        for line in result.stdout.splitlines():
            name = line.split(":", 1)[0]
            if name.startswith(prefix):
                self._run([self._config.tmux_bin, "kill-session", "-t", name])
                killed += 1
        self._sessions.clear()
        return killed

    def shutdown(self) -> int:
        """Kill every tmux REPL session owned by this pool's agent."""
        return self.kill_all(self._config.agent)

    def _get_or_spawn(  # noqa: PLR0913
        self,
        key: tuple[str, str, str],
        transport: str,
        chat: object,
        topic: object,
        model: str,
        system_prompt: str,
        effort: str = "",
    ) -> ReplSession:
        # Capture the persisted resume id *before* idle eviction can drop the
        # entry. Otherwise an evicted (idle) session falls through to the fresh
        # spawn path below with no --resume, starting a brand-new Claude session
        # and losing the prior conversation.
        existing = self._sessions.get(key)
        resume_hint = existing.session_id if existing is not None else None
        self._evict_idle()
        session = self._sessions.get(key)
        if session is not None:
            if self._session_exists(session.name):
                self._sessions.move_to_end(key)
                return session
            session = self.spawn(
                agent=self._config.agent,
                transport=transport,
                chat=chat,
                topic=topic,
                model=model,
                system_prompt=system_prompt,
                effort=effort,
                resume_session=session.session_id,
            )
            self._sessions[key] = session
            self._sessions.move_to_end(key)
            return session

        self._evict_for_capacity()
        session = self.spawn(
            agent=self._config.agent,
            transport=transport,
            chat=chat,
            topic=topic,
            model=model,
            system_prompt=system_prompt,
            effort=effort,
            resume_session=resume_hint,
        )
        self._sessions[key] = session
        return session

    def _evict_idle(self) -> None:
        cutoff = self._now() - self._config.idle_ttl_seconds
        for key, session in list(self._sessions.items()):
            if session.in_progress or session.last_active >= cutoff:
                continue
            self._kill_session(session.name)
            del self._sessions[key]

    def _evict_for_capacity(self) -> None:
        while len(self._sessions) >= self._config.max_sessions:
            for key, session in self._sessions.items():
                if not session.in_progress:
                    self._kill_session(session.name)
                    del self._sessions[key]
                    break
            else:
                raise ReplSessionBusyError("all REPL sessions are in progress")

    def _container_working_dir(self) -> Path:
        return self._config.container_working_dir or self._config.working_dir

    def _container_claude_home(self) -> Path:
        return self._config.container_claude_home or self._config.claude_home

    def _container_signal_dir(self) -> Path:
        return self._config.container_signal_dir or self._config.signal_dir

    def _claude_env(self, transport: str, chat: str, topic: str) -> dict[str, str]:
        """Return the allowlisted environment for the long-lived Claude process.

        The allowlist keeps ``env -i`` pure (no os.environ leak); the DUCTOR_*
        vars come from the shared ``build_ductor_env`` helper so the REPL turn
        sees the same agent/inter-agent/home env the -p path sets. Docker-mode
        adds the inter-agent host the container uses to reach the host bus.
        """
        env = {name: os.environ[name] for name in self._config.env_allowlist if name in os.environ}
        env.setdefault("PATH", _DEFAULT_PATH)
        env["CLAUDE_CONFIG_DIR"] = str(self._container_claude_home())
        env.update(
            build_ductor_env(
                agent_name=self._config.agent,
                interagent_port=self._config.interagent_port,
                transport=transport,
                chat_id=chat,
                topic_id=topic if topic and topic != "none" else "",
                ductor_home=self._config.ductor_home,
                shared_memory_path=self._config.shared_memory_path,
                transcribe_command=self._config.transcribe_command,
                video_transcribe_command=self._config.video_transcribe_command,
            )
        )
        if self._config.docker_container:
            env["DUCTOR_INTERAGENT_HOST"] = DOCKER_INTERAGENT_HOST
        return env

    def _claude_command(  # noqa: PLR0913
        self,
        model: str,
        system_prompt: str,
        resume_session: str | None,
        transport: str,
        chat: str,
        topic: str,
        effort: str = "",
    ) -> str:
        env_argv = [
            "env",
            "-i",
            *(
                f"{key}={value}"
                for key, value in sorted(self._claude_env(transport, chat, topic).items())
            ),
        ]
        argv = [
            *env_argv,
            self._config.claude_bin,
            "--permission-mode",
            self._config.permission_mode or "default",
            "--model",
            model,
            "--append-system-prompt",
            system_prompt,
        ]
        if effort and effort != "default":
            argv.extend(["--effort", effort])
        if self._config.tool_denylist:
            argv.extend(["--disallowedTools", *self._config.tool_denylist])
        if resume_session:
            argv.extend(["--resume", resume_session])
        return shlex.join(argv)

    def _handle_boot_prompts(self, tmux_name: str) -> None:
        deadline = self._now() + self._config.boot_timeout_seconds
        seen_actions = 0
        while self._now() < deadline:
            screen = self._capture_pane(tmux_name)
            actions = boot_actions_for_screen(screen)
            if not actions:
                self._sleep(0.2)
                if seen_actions:
                    return
                continue
            for index, action in enumerate(actions):
                if index:
                    self._sleep(self._config.boot_key_delay)
                self._send_key(tmux_name, action)
            seen_actions += 1
            self._sleep(0.5)

    def _send_text(self, tmux_name: str, text: str) -> None:
        if "\n" in text or len(text) > self._config.paste_threshold:
            buffer_name = f"ductor-{secrets.token_hex(4)}"
            try:
                self._run([self._config.tmux_bin, "load-buffer", "-b", buffer_name, "-"], text)
                self._run(
                    [
                        self._config.tmux_bin,
                        "paste-buffer",
                        "-t",
                        tmux_name,
                        "-b",
                        buffer_name,
                        "-p",
                    ]
                )
            finally:
                self._call([self._config.tmux_bin, "delete-buffer", "-b", buffer_name], None)
        else:
            self._run([self._config.tmux_bin, "send-keys", "-t", tmux_name, "-l", text])
        self._send_key(tmux_name, "Enter")

    def _send_key(self, tmux_name: str, key: str) -> None:
        self._run([self._config.tmux_bin, "send-keys", "-t", tmux_name, key])

    def _capture_pane(self, tmux_name: str) -> str:
        result = self._run([self._config.tmux_bin, "capture-pane", "-p", "-t", tmux_name])
        return result.stdout

    def _wait_for_signal(
        self,
        *,
        nonce: str,
        session_id: str | None,
        tmux_name: str | None = None,
        baseline_screen: str | None = None,
        timeout_seconds: float | None = None,
    ) -> _SignalMatch:
        deadline = self._now() + (timeout_seconds or self._config.signal_timeout_seconds)
        ticks = 0
        while self._now() < deadline:
            match = self._find_signal(nonce=nonce, session_id=session_id)
            if match is not None:
                return match
            # Active failure detection (~every 2s): a turn that can never
            # produce a Stop-hook signal should abort fast, not block the
            # full timeout. Check the signal file first so a just-finished
            # turn is never misreported as a failure.
            ticks += 1
            if tmux_name is not None and ticks % 20 == 0:
                if not self._session_exists(tmux_name):
                    if self._find_signal(nonce=nonce, session_id=session_id) is None:
                        raise ReplFatalError("REPL process exited before completing the turn")
                else:
                    fatal = fatal_error_for_screen(self._capture_pane(tmux_name))
                    # Ignore a marker already present before this turn (stale
                    # error redrawn by --resume); only a newly-appeared marker
                    # means the current turn failed.
                    baseline_fatal = fatal_error_for_screen(baseline_screen or "")
                    if (
                        fatal is not None
                        and baseline_fatal is None
                        and self._find_signal(nonce=nonce, session_id=session_id) is None
                    ):
                        raise ReplFatalError(f"REPL turn failed: {fatal}")
            self._sleep(0.1)
        raise ReplTimeoutError(f"timed out waiting for REPL Stop hook signal: {nonce}")

    def _find_signal(self, *, nonce: str, session_id: str | None) -> _SignalMatch | None:
        agent_dir = self._config.signal_dir / self._config.agent
        if session_id:
            path = agent_dir / f"{session_id}.{nonce}.done"
            return _SignalMatch(path=path, session_id=session_id) if path.exists() else None
        pattern = f"*.{nonce}.done"
        for path in agent_dir.glob(pattern):
            return _SignalMatch(path=path, session_id=path.name[: -len(f".{nonce}.done")])
        return None

    def _transcript_size(self, session: ReplSession) -> int:
        if session.transcript_path is None and session.session_id:
            session.transcript_path = self._find_transcript(session.session_id, required=False)
        if session.transcript_path is None or not session.transcript_path.exists():
            return 0
        return session.transcript_path.stat().st_size

    def _find_transcript(self, session_id: str, *, required: bool = True) -> Path | None:
        projects = self._config.claude_home / "projects"
        for path in projects.glob(f"**/{session_id}.jsonl"):
            return path
        if required:
            raise FileNotFoundError(f"Claude transcript not found for session {session_id}")
        return None

    def _session_exists(self, tmux_name: str) -> bool:
        result = self._call([self._config.tmux_bin, "has-session", "-t", tmux_name], None)
        return result.returncode == 0

    def _kill_session(self, tmux_name: str) -> None:
        self._call([self._config.tmux_bin, "kill-session", "-t", tmux_name], None)

    def _wrap_command(self, args: Sequence[str], input_text: str | None) -> list[str]:
        argv = list(args)
        if argv[:1] != [self._config.tmux_bin] or not self._config.docker_container:
            return argv
        docker_args = ["docker", "exec"]
        if input_text is not None:
            docker_args.append("-i")
        return [*docker_args, self._config.docker_container, *argv]

    def _call(
        self, args: Sequence[str], input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(self._wrap_command(args, input_text), input_text)

    def _run(
        self, args: Sequence[str], input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        wrapped = self._wrap_command(args, input_text)
        result = self._runner(wrapped, input_text)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(f"command failed: {shlex.join(wrapped)}: {stderr}")
        return result
