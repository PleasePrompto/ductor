"""Helpers for handing ductor-managed Codex sessions back to the desktop TUI."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ductor_bot.infra.json_store import load_json
from ductor_bot.workspace.paths import DuctorPaths


@dataclass(frozen=True, slots=True)
class CodexResumeTarget:
    """A Codex session known to ductor that can be resumed in the desktop TUI."""

    label: str
    target: str
    session_id: str
    working_dir: str
    model: str
    source_kind: str
    updated_at: float
    chat_id: int
    topic_id: int | None = None


def build_codex_resume_args(session_id: str, working_dir: str = "") -> list[str]:
    """Build argv for the interactive Codex resume command."""
    args = ["codex", "resume", "--include-non-interactive", "--all"]
    if working_dir:
        args += ["--cd", working_dir]
    args.append(session_id)
    return args


def build_codex_resume_command(session_id: str, working_dir: str = "") -> str:
    """Return a shell-safe command string for resuming a ductor session on desktop."""
    return " ".join(shlex.quote(part) for part in build_codex_resume_args(session_id, working_dir))


def run_codex_resume(session_id: str, working_dir: str = "") -> int:
    """Launch the desktop Codex TUI for *session_id* and return its process code."""
    proc = subprocess.run(build_codex_resume_args(session_id, working_dir), check=False)
    return proc.returncode


def load_resume_targets(paths: DuctorPaths) -> list[CodexResumeTarget]:
    """Load all ductor-known Codex sessions that have a resumable session id."""
    targets: list[CodexResumeTarget] = []
    targets.extend(_load_main_targets(paths))
    targets.extend(_load_named_targets(paths))
    return sorted(targets, key=lambda item: (-item.updated_at, item.label))


def find_resume_target(paths: DuctorPaths, selector: str) -> CodexResumeTarget | None:
    """Find a target by ``@name`` or exact target id."""
    normalized = selector.strip()
    targets = load_resume_targets(paths)
    for target in targets:
        if normalized == target.target:
            return target
        if target.target.startswith("@") and normalized == target.target[1:]:
            return target
    return None


def latest_resume_target(paths: DuctorPaths, *, main_only: bool = False) -> CodexResumeTarget | None:
    """Return the newest known Codex target, optionally limited to main chat sessions."""
    targets = load_resume_targets(paths)
    if main_only:
        targets = [target for target in targets if target.target.startswith("main:")]
    return targets[0] if targets else None


def _load_main_targets(paths: DuctorPaths) -> list[CodexResumeTarget]:
    raw = load_json(paths.sessions_path)
    if not isinstance(raw, dict):
        return []
    targets: list[CodexResumeTarget] = []
    for storage_key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        provider_sessions = entry.get("provider_sessions")
        if not isinstance(provider_sessions, dict):
            continue
        codex = provider_sessions.get("codex")
        if not isinstance(codex, dict):
            continue
        session_id = str(codex.get("session_id", "") or "")
        if not session_id:
            continue
        chat_id = _safe_int(entry.get("chat_id", 0))
        topic_id = _optional_int(entry.get("topic_id"))
        topic_name = str(entry.get("topic_name", "") or "")
        label = _main_label(chat_id=chat_id, topic_id=topic_id, topic_name=topic_name)
        source_kind = str(codex.get("source_kind", "ductor") or "ductor")
        targets.append(
            CodexResumeTarget(
                label=label,
                target=f"main:{storage_key}",
                session_id=session_id,
                working_dir=_resume_working_dir(
                    str(codex.get("working_dir", "") or ""),
                    source_kind=source_kind,
                    paths=paths,
                ),
                model=str(entry.get("model", "") or ""),
                source_kind=source_kind,
                updated_at=_parse_updated(entry.get("last_active")),
                chat_id=chat_id,
                topic_id=topic_id,
            )
        )
    return targets


def _load_named_targets(paths: DuctorPaths) -> list[CodexResumeTarget]:
    raw = load_json(paths.named_sessions_path)
    if not isinstance(raw, dict):
        return []
    sessions = raw.get("sessions", [])
    if not isinstance(sessions, list):
        return []
    targets: list[CodexResumeTarget] = []
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status", "")) == "ended":
            continue
        if str(entry.get("provider", "")) != "codex":
            continue
        session_id = str(entry.get("session_id", "") or "")
        name = str(entry.get("name", "") or "")
        if not session_id or not name:
            continue
        source_kind = str(entry.get("source_kind", "ductor") or "ductor")
        targets.append(
            CodexResumeTarget(
                label=f"@{name}",
                target=f"@{name}",
                session_id=session_id,
                working_dir=_resume_working_dir(
                    str(entry.get("working_dir", "") or ""),
                    source_kind=source_kind,
                    paths=paths,
                ),
                model=str(entry.get("model", "") or ""),
                source_kind=source_kind,
                updated_at=float(entry.get("created_at", 0.0) or 0.0),
                chat_id=_safe_int(entry.get("chat_id", 0)),
            )
        )
    return targets


def _main_label(*, chat_id: int, topic_id: int | None, topic_name: str) -> str:
    if topic_id is None:
        return f"main chat {chat_id}"
    if topic_name:
        return f"topic {topic_name} ({chat_id}/{topic_id})"
    return f"topic {chat_id}/{topic_id}"


def _resume_working_dir(raw: str, *, source_kind: str, paths: DuctorPaths) -> str:
    if raw:
        return raw
    if source_kind == "ductor":
        return str(paths.workspace)
    return ""


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_updated(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0
