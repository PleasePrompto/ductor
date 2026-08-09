"""Read resumable Codex session metadata from the local Codex home."""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CodexHistorySession:
    """One resumable Codex session."""

    session_id: str
    thread_name: str
    updated_at: str
    updated_ts: float
    working_dir: str
    preview: str
    source: str
    cli_version: str
    summary: str
    first_prompt: str = ""
    last_reply: str = ""
    last_output_summary: str = ""
    turn_count: int = 0
    model: str = ""
    launch_dir: str = ""
    is_ductor_touched: bool = False
    is_ductor_task: bool = False
    is_subagent: bool = False


@dataclass(frozen=True, slots=True)
class CodexHistoryProject:
    """A project group keyed by working directory."""

    working_dir: str
    label: str
    updated_at: str
    updated_ts: float
    sessions: tuple[CodexHistorySession, ...]


@dataclass(frozen=True, slots=True)
class CodexHistoryBrowser:
    """Grouped Codex history view used by the Telegram selector."""

    projects: tuple[CodexHistoryProject, ...]
    skipped_count: int = 0
    history_available: bool = False


@dataclass(frozen=True, slots=True)
class _SessionMeta:
    """Metadata derived from a local Codex session file."""

    working_dir: str
    source: str
    cli_version: str
    updated_at: str
    updated_ts: float
    last_reply: str
    model: str
    latest_prompt: str


@dataclass(frozen=True, slots=True)
class _HistorySummary:
    """Prompt history derived from Codex history.jsonl."""

    first_prompt: str
    latest_prompt: str
    prompt_count: int
    first_ts: float
    latest_ts: float


@dataclass(frozen=True, slots=True)
class _ThreadState:
    """Thread metadata from Codex's current state database."""

    title: str
    first_user_message: str
    raw_title: str
    raw_first_user_message: str
    updated_at: str
    updated_ts: float
    source: str
    cli_version: str
    model: str


@dataclass(frozen=True, slots=True)
class _DuctorTaskState:
    """Ductor task metadata for a Codex background-worker session."""

    task_id: str
    name: str
    parent_prompt_preview: str
    prompt_preview: str
    result_preview: str


@dataclass(frozen=True, slots=True)
class _DuctorSessionTouches:
    """Ductor-owned/touched Codex session IDs."""

    owned_session_ids: frozenset[str]
    latest_prompt_ts_by_session: dict[str, float]


def codex_home() -> Path:
    """Return the effective Codex home directory."""
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def load_codex_history_browser(home: Path | None = None) -> CodexHistoryBrowser:
    """Load recent Codex sessions grouped by recorded working directory."""
    codex_dir = home or codex_home()
    index_path = codex_dir / "session_index.jsonl"
    history_path = codex_dir / "history.jsonl"
    sessions_dir = codex_dir / "sessions"
    state_path = codex_dir / "state_5.sqlite"
    history_available = index_path.exists() or history_path.exists() or sessions_dir.exists()
    if not sessions_dir.exists():
        return CodexHistoryBrowser(projects=(), skipped_count=0, history_available=history_available)

    index_rows = _read_session_index(index_path) if index_path.exists() else {}
    history = _read_history_summaries(history_path) if history_path.exists() else {}
    thread_state = _read_thread_state(state_path) if state_path.exists() else {}
    ductor_tasks = _read_ductor_task_state()
    ductor_touches = _read_ductor_session_touches()
    meta = _read_session_meta(sessions_dir)

    grouped: dict[str, list[CodexHistorySession]] = {}
    skipped_count = 0
    for session_id, meta_row in meta.items():
        index_row = index_rows.get(session_id)
        history_row = history.get(session_id)
        state_row = thread_state.get(session_id)
        task_row = ductor_tasks.get(session_id)
        preview = history_row.latest_prompt if history_row is not None else meta_row.latest_prompt
        first_prompt = history_row.first_prompt if history_row is not None else ""
        turn_count = history_row.prompt_count if history_row is not None else 0
        state_title = state_row.title if state_row is not None else ""
        state_first_prompt = state_row.first_user_message if state_row is not None else ""
        working_dir = _semantic_working_dir(
            meta_row.working_dir,
            state_row.raw_title if state_row is not None else "",
            state_row.raw_first_user_message if state_row is not None else "",
            state_title,
            state_first_prompt,
            first_prompt,
            preview,
            task_row.prompt_preview if task_row is not None else "",
            task_row.result_preview if task_row is not None else "",
            meta_row.last_reply,
        )
        thread_name = _choose_thread_name(
            session_id=session_id,
            candidates=(
                _task_display_title(task_row) if task_row is not None else "",
                index_row[0] if index_row is not None else "",
                state_title,
                state_first_prompt,
                first_prompt,
                preview,
            ),
        )
        updated_at, updated_ts = _preferred_updated(index_row, meta_row, history_row, state_row)
        latest_prompt_ts = history_row.latest_ts if history_row is not None else 0.0
        session_source = (
            f"task:{task_row.task_id}"
            if task_row is not None
            else meta_row.source or (state_row.source if state_row is not None else "")
        )
        is_ductor_touched = _is_ductor_touched_session(
            session_id=session_id,
            task_row=task_row,
            meta_row=meta_row,
            latest_prompt_ts=latest_prompt_ts,
            ductor_touches=ductor_touches,
        )
        is_subagent = session_source.startswith("subagent:")
        if not _is_attachable_cwd(working_dir):
            skipped_count += 1
            continue
        summary = thread_name if not preview else f"{thread_name} | {preview}"
        session = CodexHistorySession(
            session_id=session_id,
            thread_name=thread_name,
            updated_at=updated_at,
            updated_ts=updated_ts,
            working_dir=working_dir,
            preview=preview,
            source=session_source,
            cli_version=meta_row.cli_version or (state_row.cli_version if state_row is not None else ""),
            summary=summary,
            first_prompt=first_prompt or state_first_prompt,
            last_reply=meta_row.last_reply,
            last_output_summary=_summarize_output(meta_row.last_reply),
            turn_count=turn_count,
            model=meta_row.model or (state_row.model if state_row is not None else ""),
            launch_dir=meta_row.working_dir if meta_row.working_dir != working_dir else "",
            is_ductor_touched=is_ductor_touched,
            is_ductor_task=task_row is not None,
            is_subagent=is_subagent,
        )
        grouped.setdefault(working_dir, []).append(session)

    projects: list[CodexHistoryProject] = []
    for working_dir, sessions in grouped.items():
        ordered = tuple(sorted(sessions, key=lambda item: (_session_sort_rank(item), -item.updated_ts, item.session_id)))
        latest = max(ordered, key=lambda item: item.updated_ts)
        projects.append(
            CodexHistoryProject(
                working_dir=working_dir,
                label=Path(working_dir).name or working_dir,
                updated_at=latest.updated_at,
                updated_ts=latest.updated_ts,
                sessions=ordered,
            )
        )

    projects.sort(key=lambda item: (-item.updated_ts, item.working_dir))
    return CodexHistoryBrowser(
        projects=tuple(projects),
        skipped_count=skipped_count,
        history_available=history_available,
    )


def _session_sort_rank(session: CodexHistorySession) -> int:
    if session.is_ductor_task:
        return 2
    if session.is_subagent:
        return 1
    return 0


def _read_session_index(path: Path) -> dict[str, tuple[str, str, float]]:
    rows: dict[str, tuple[str, str, float]] = {}
    for raw in _iter_jsonl(path):
        session_id = str(raw.get("id", "") or "")
        if not session_id:
            continue
        thread_name = html.unescape(str(raw.get("thread_name", "") or "")).strip()
        updated_at = str(raw.get("updated_at", "") or "")
        updated_ts = _parse_ts(updated_at)
        rows[session_id] = (thread_name or session_id, updated_at, updated_ts)
    return rows


def _read_history_summaries(path: Path) -> dict[str, _HistorySummary]:
    by_session: dict[str, tuple[float, str, float, str, int]] = {}
    for raw in _iter_jsonl(path):
        session_id = str(raw.get("session_id", "") or "")
        text = _clean_snippet(str(raw.get("text", "") or ""))
        if not session_id or not text:
            continue
        ts_raw = raw.get("ts", 0)
        if isinstance(ts_raw, (int, float)):
            ts = float(ts_raw)
        elif isinstance(ts_raw, str):
            try:
                ts = float(ts_raw)
            except ValueError:
                ts = 0.0
        else:
            ts = 0.0
        current = by_session.get(session_id)
        if current is None:
            by_session[session_id] = (ts, text, ts, text, 1)
            continue
        first_ts, first_prompt, latest_ts, latest_prompt, count = current
        if ts < first_ts:
            first_ts, first_prompt = ts, text
        if ts >= latest_ts:
            latest_ts, latest_prompt = ts, text
        by_session[session_id] = (first_ts, first_prompt, latest_ts, latest_prompt, count + 1)
    return {
        session_id: _HistorySummary(
            first_prompt=first_prompt,
            latest_prompt=latest_prompt,
            prompt_count=count,
            first_ts=_first_ts,
            latest_ts=_latest_ts,
        )
        for session_id, (_first_ts, first_prompt, _latest_ts, latest_prompt, count) in by_session.items()
    }


def _read_thread_state(path: Path) -> dict[str, _ThreadState]:
    rows: dict[str, _ThreadState] = {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return rows
    try:
        con.row_factory = sqlite3.Row
        for raw in con.execute(
            """
            SELECT id, title, first_user_message, updated_at, updated_at_ms,
                   source, cli_version, model
            FROM threads
            """
        ):
            session_id = str(raw["id"] or "")
            if not session_id:
                continue
            updated_ts = _thread_updated_ts(raw["updated_at"], raw["updated_at_ms"])
            raw_title = str(raw["title"] or "")
            raw_first_user_message = str(raw["first_user_message"] or "")
            rows[session_id] = _ThreadState(
                title=_clean_prompt_title(raw_title),
                first_user_message=_clean_prompt_title(raw_first_user_message),
                raw_title=raw_title,
                raw_first_user_message=raw_first_user_message,
                updated_at=_format_ts(updated_ts),
                updated_ts=updated_ts,
                source=_clean_snippet(str(raw["source"] or ""), limit=40),
                cli_version=_clean_snippet(str(raw["cli_version"] or ""), limit=40),
                model=_clean_snippet(str(raw["model"] or ""), limit=80),
            )
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return rows


def _read_ductor_task_state() -> dict[str, _DuctorTaskState]:
    raw = _read_json_file(_ductor_tasks_path())
    if not isinstance(raw, dict):
        return {}
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        return {}
    historical_parent_prompts = _read_historical_parent_prompts()
    rows: dict[str, _DuctorTaskState] = {}
    for item in tasks:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id", "") or "")
        task_id = str(item.get("task_id", "") or "")
        name = _clean_snippet(str(item.get("name", "") or ""), limit=120)
        stored_parent_prompt = _clean_snippet(str(item.get("parent_prompt_preview", "") or ""), limit=160)
        historical_parent_prompt = historical_parent_prompts.get(task_id, "")
        parent_prompt = _choose_task_parent_prompt(stored_parent_prompt, historical_parent_prompt)
        if not session_id or not task_id or not name:
            continue
        rows[session_id] = _DuctorTaskState(
            task_id=task_id,
            name=name,
            parent_prompt_preview=parent_prompt,
            prompt_preview=_clean_snippet(str(item.get("prompt_preview", "") or ""), limit=500),
            result_preview=_clean_snippet(str(item.get("result_preview", "") or ""), limit=1000),
        )
    return rows


def _read_ductor_session_touches() -> _DuctorSessionTouches:
    ductor_home = _ductor_tasks_path().parent
    owned = set(_read_owned_session_ids(ductor_home / "sessions.json"))
    owned.update(_read_owned_named_session_ids(ductor_home / "named_sessions.json"))
    latest_prompt_ts_by_session = _read_ductor_prompt_session_timestamps(ductor_home / "logs")
    return _DuctorSessionTouches(
        owned_session_ids=frozenset(owned),
        latest_prompt_ts_by_session=latest_prompt_ts_by_session,
    )


def _read_owned_session_ids(path: Path) -> set[str]:
    raw = _read_json_file(path)
    if not isinstance(raw, dict):
        return set()
    owned: set[str] = set()
    for item in raw.values():
        if not isinstance(item, dict):
            continue
        providers = item.get("provider_sessions")
        if not isinstance(providers, dict):
            continue
        codex = providers.get("codex")
        if not isinstance(codex, dict):
            continue
        session_id = str(codex.get("session_id", "") or "")
        if session_id and str(codex.get("source_kind", "") or "") == "ductor":
            owned.add(session_id)
    return owned


def _read_owned_named_session_ids(path: Path) -> set[str]:
    raw = _read_json_file(path)
    if not isinstance(raw, dict):
        return set()
    entries = raw.get("sessions")
    if not isinstance(entries, list):
        return set()
    owned: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id", "") or "")
        if session_id and str(item.get("provider", "") or "") == "codex" and str(item.get("source_kind", "") or "") == "ductor":
            owned.add(session_id)
    return owned


def _read_ductor_prompt_session_timestamps(log_dir: Path) -> dict[str, float]:
    if not log_dir.is_dir():
        return {}
    latest_by_session: dict[str, float] = {}
    for path in _agent_log_paths(log_dir):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            session_id = _ductor_codex_resume_session_id(line)
            if not session_id:
                continue
            ts = _line_timestamp(line)
            if ts >= latest_by_session.get(session_id, 0.0):
                latest_by_session[session_id] = ts
    return latest_by_session


def _ductor_codex_resume_session_id(line: str) -> str:
    if "ductor_bot.cli.codex_provider" not in line or "codex exec resume" not in line:
        return ""
    match = re.search(
        r"\bcodex\s+exec\s+resume\b.*?\s--\s+"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
        line,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _is_ductor_touched_session(
    *,
    session_id: str,
    task_row: _DuctorTaskState | None,
    meta_row: _SessionMeta,
    latest_prompt_ts: float,
    ductor_touches: _DuctorSessionTouches,
) -> bool:
    if task_row is not None or session_id in ductor_touches.owned_session_ids:
        return True
    prompt_ts = ductor_touches.latest_prompt_ts_by_session.get(session_id, 0.0)
    if prompt_ts <= 0:
        return _is_ductor_workspace_path(meta_row.working_dir) and meta_row.source.startswith("exec")
    if latest_prompt_ts <= 0:
        return True
    return prompt_ts >= latest_prompt_ts - 60


def _task_display_title(task: _DuctorTaskState) -> str:
    parent = _clean_snippet(task.parent_prompt_preview, limit=90)
    name = _clean_snippet(task.name, limit=80)
    if parent and name and name.casefold() not in parent.casefold():
        return f"{name} / {parent}"
    return parent or name


def _choose_task_parent_prompt(stored: str, historical: str) -> str:
    if stored and not _is_historical_continuation_prompt(stored):
        return stored
    return historical or stored


def _read_historical_parent_prompts() -> dict[str, str]:
    log_dir = _ductor_tasks_path().parent / "logs"
    if not log_dir.is_dir():
        return {}
    parent_by_task: dict[str, str] = {}
    latest_meaningful_prompt = ""
    active_root_prompt = ""
    latest_prompt_ts = 0.0
    for path in _agent_log_paths(log_dir):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            prompt, prompt_ts = _historical_message_prompt(line)
            if prompt:
                active_root_prompt, latest_meaningful_prompt, latest_prompt_ts = _merge_historical_prompt(
                    prompt,
                    prompt_ts,
                    active_root_prompt=active_root_prompt,
                    latest_meaningful_prompt=latest_meaningful_prompt,
                    latest_prompt_ts=latest_prompt_ts,
                )
                continue
            task_id = _historical_task_id(line)
            if task_id:
                parent = active_root_prompt or latest_meaningful_prompt
                if parent:
                    parent_by_task.setdefault(task_id, parent)
    return parent_by_task


def _agent_log_paths(log_dir: Path) -> list[Path]:
    def sort_key(path: Path) -> int:
        suffix = path.name.removeprefix("agent.log")
        if suffix.startswith(".") and suffix[1:].isdigit():
            return -int(suffix[1:])
        return 0

    return sorted(log_dir.glob("agent.log*"), key=sort_key)


def _historical_message_prompt(line: str) -> tuple[str, float]:
    prompt_match = re.search(r"Message received text=(.*)$", line)
    if not prompt_match:
        return "", 0.0
    prompt = _clean_snippet(prompt_match.group(1), limit=160)
    if not _is_meaningful_parent_prompt(prompt):
        return "", 0.0
    return prompt, _line_timestamp(line)


def _merge_historical_prompt(
    prompt: str,
    prompt_ts: float,
    *,
    active_root_prompt: str,
    latest_meaningful_prompt: str,
    latest_prompt_ts: float,
) -> tuple[str, str, float]:
    gap = prompt_ts - latest_prompt_ts if prompt_ts and latest_prompt_ts else 0.0
    if _starts_historical_workstream(prompt, gap, active_root_prompt):
        active_root_prompt = prompt
    if prompt_ts:
        latest_prompt_ts = prompt_ts
    return active_root_prompt, prompt or latest_meaningful_prompt, latest_prompt_ts


def _historical_task_id(line: str) -> str:
    task_match = re.search(r"Task created id=([0-9a-f]+)", line)
    return task_match.group(1) if task_match else ""


def _is_meaningful_parent_prompt(raw: str) -> bool:
    value = raw.strip()
    if not value or value.startswith("[BACKGROUND TASK COMPLETED"):
        return False
    lower = value.casefold().strip(". ")
    if lower in {"proceed", "ok", "okay", "yes", "yep", "implement"}:
        return False
    return not (lower.startswith("proceed") and len(lower.split()) <= 4)


def _starts_historical_workstream(raw: str, gap_seconds: float, active_root: str) -> bool:
    if not active_root:
        return True
    if _is_historical_continuation_prompt(raw):
        return False
    if gap_seconds >= 7200:
        return True
    lower = raw.casefold()
    return lower.startswith(
        (
            "/plan ",
            "review ",
            "can you review ",
            "how come ",
            "why ",
            "i just want ",
            "so how do i ",
            "let's ",
        )
    )


def _is_historical_continuation_prompt(raw: str) -> bool:
    lower = raw.casefold().strip()
    lower = lower.removeprefix("/implement ").removeprefix("/plan ").strip()
    if not lower:
        return True
    prefixes = (
        "and ",
        "but ",
        "that ",
        "this ",
        "ok ",
        "ok. ",
        "okay ",
        "okay. ",
        "yep ",
        "yep. ",
        "yeah ",
        "yeah. ",
        "so could ",
        "you will ",
        "we would ",
        "close out ",
        "can you pick up ",
        "can you do that ",
    )
    if lower.startswith(prefixes):
        return True
    return any(
        marker in lower
        for marker in (
            " next then",
            "next obvious milestone",
            "this change",
            "alias-safe baseline next",
        )
    )


def _line_timestamp(line: str) -> float:
    match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    if not match:
        return 0.0
    try:
        return datetime.fromisoformat(match.group(1)).timestamp()
    except ValueError:
        return 0.0


def _ductor_tasks_path() -> Path:
    default = Path.home() / ".ductor" / "tasks.json"
    configured = Path(os.environ.get("DUCTOR_HOME", "")).expanduser()
    if configured:
        candidate = configured / "tasks.json"
        if candidate.is_file():
            return candidate
    return default


def _read_json_file(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_session_meta(path: Path) -> dict[str, _SessionMeta]:
    meta: dict[str, _SessionMeta] = {}
    for session_file in sorted(path.rglob("*.jsonl")):
        parsed = _read_single_session_meta(session_file)
        if parsed is None:
            continue
        session_id, candidate = parsed
        current = meta.get(session_id)
        if current is None or candidate.updated_ts >= current.updated_ts:
            meta[session_id] = candidate
    return meta


def _read_single_session_meta(session_file: Path) -> tuple[str, _SessionMeta] | None:
    try:
        with session_file.open("r", encoding="utf-8") as handle:
            first_line = handle.readline().strip()
            if not first_line:
                return None
            row = _parse_json_line(first_line)
            if row is None:
                return None
            parsed = _parse_session_meta_header(row)
            if parsed is None:
                return None
            session_id, working_dir, source, cli_version, updated_at, updated_ts = parsed
            updated_at, updated_ts, last_reply, model, latest_prompt = _scan_session_events(
                session_file,
                initial_updated_at=updated_at,
                initial_updated_ts=updated_ts,
            )
    except OSError:
        return None

    if updated_ts <= 0:
        try:
            updated_ts = session_file.stat().st_mtime
        except OSError:
            updated_ts = 0.0
        updated_at = _format_ts(updated_ts)

    return session_id, _SessionMeta(
        working_dir=working_dir,
        source=source,
        cli_version=cli_version,
        updated_at=updated_at,
        updated_ts=updated_ts,
        last_reply=last_reply,
        model=model,
        latest_prompt=latest_prompt,
    )


def _parse_session_meta_header(
    row: dict[str, object],
) -> tuple[str, str, str, str, str, float] | None:
    if row.get("type") != "session_meta":
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    session_id = str(payload.get("id", "") or "")
    working_dir = str(payload.get("cwd", "") or "")
    if not session_id or not working_dir:
        return None
    source = _format_source(payload.get("source"))
    cli_version = str(payload.get("cli_version", "") or "")
    updated_at = _clean_timestamp(str(row.get("timestamp", "") or ""))
    return session_id, working_dir, source, cli_version, updated_at, _parse_ts(updated_at)


def _scan_session_events(
    session_file: Path,
    *,
    initial_updated_at: str,
    initial_updated_ts: float,
) -> tuple[str, float, str, str, str]:
    updated_at = initial_updated_at
    updated_ts = initial_updated_ts
    last_reply = ""
    model = ""
    latest_prompt = ""
    for item in _iter_tail_jsonl(session_file):
        if item is None:
            continue
        updated_at, updated_ts = _merge_updated(item, updated_at=updated_at, updated_ts=updated_ts)
        prompt, message, model_value = _extract_session_event_values(item)
        if prompt:
            latest_prompt = prompt
            continue
        if message:
            last_reply = message
        if model_value:
            model = model_value
    return updated_at, updated_ts, last_reply, model, latest_prompt


def _extract_session_event_values(item: dict[str, object]) -> tuple[str, str, str]:
    payload = item.get("payload")
    item_type = str(item.get("type", "") or "")
    if item_type == "event_msg":
        prompt = _extract_event_user_message(payload)
        message = "" if prompt else _extract_event_agent_message(payload)
        return prompt, message, ""
    if item_type == "response_item":
        prompt = _extract_user_input(payload)
        message = "" if prompt else _extract_assistant_output(payload)
        return prompt, message, ""
    if item_type == "turn_context":
        return "", "", _extract_turn_context_model(payload)
    return "", "", ""


def _iter_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return []
    return rows


def _is_attachable_cwd(raw_cwd: str) -> bool:
    path = Path(raw_cwd).expanduser()
    return path.is_dir() and os.access(path, os.R_OK | os.X_OK)


def _parse_ts(raw: str) -> float:
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw).astimezone(UTC).timestamp()
    except ValueError:
        return 0.0


def _format_ts(ts: float) -> str:
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _clean_timestamp(raw: str) -> str:
    return raw.strip()


def _clean_snippet(raw: str, *, limit: int = 160) -> str:
    cleaned = html.unescape(raw).strip()
    if not cleaned:
        return ""
    return " ".join(cleaned.split())[:limit]


def _clean_prompt_title(raw: str, *, limit: int = 160) -> str:
    cleaned = html.unescape(raw).strip()
    if not cleaned:
        return ""
    for marker in (
        "\n\n## BACKGROUND TASKS",
        "\n\n## MEMORY CHECK",
        "\n\n# Main Memory",
        "\n\n---\nTASK RULES",
        "\n---\nTASK RULES",
        "\n\n<environment_context>",
    ):
        head, sep, _tail = cleaned.partition(marker)
        if sep:
            cleaned = head
            break
    if cleaned.startswith("# AGENTS.md instructions"):
        return ""
    task_head, task_sep, task_tail = cleaned.partition("Task:")
    if task_sep and "Context:" in task_head[:80]:
        cleaned = task_tail.strip()
    return _clean_snippet(cleaned, limit=limit)


def _summarize_output(raw: str, *, limit: int = 110) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    lines = [line.strip(" -*>\t") for line in text.splitlines()]
    candidates = [line for line in lines if line and not line.endswith(":")]
    collapsed = " ".join(candidates) if candidates else " ".join(text.split())
    collapsed = " ".join(collapsed.split())
    if not collapsed:
        return ""
    for separator in (". ", "; ", " - "):
        head, sep, _tail = collapsed.partition(separator)
        if sep and 20 <= len(head) <= limit:
            collapsed = head
            break
    if len(collapsed) <= limit:
        return collapsed
    clipped = collapsed[: limit - 1].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return f"{clipped}…"


def _extract_assistant_output(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("type") != "message" or payload.get("role") != "assistant":
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "output_text":
            continue
        text = _clean_snippet(str(item.get("text", "") or ""), limit=1000)
        if text:
            chunks.append(text)
    if not chunks:
        return ""
    return " ".join(chunks)[:1000]


def _extract_user_input(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("type") != "message" or payload.get("role") != "user":
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "input_text":
            continue
        text = _clean_visible_user_prompt(str(item.get("text", "") or ""))
        if text:
            chunks.append(text)
    if not chunks:
        return ""
    return " ".join(chunks)[:240]


def _extract_event_agent_message(payload: object) -> str:
    if not isinstance(payload, dict) or payload.get("type") != "agent_message":
        return ""
    return _clean_snippet(str(payload.get("message", "") or ""), limit=1000)


def _extract_event_user_message(payload: object) -> str:
    if not isinstance(payload, dict) or payload.get("type") != "user_message":
        return ""
    return _clean_visible_user_prompt(str(payload.get("message", "") or ""))


def _clean_visible_user_prompt(raw: str) -> str:
    prompt = _clean_prompt_title(raw, limit=240)
    if not prompt:
        return ""
    if prompt.startswith("[BACKGROUND TASK COMPLETED"):
        return ""
    return prompt


def _semantic_working_dir(raw_cwd: str, *texts: str) -> str:
    cwd = str(Path(raw_cwd).expanduser())
    if not cwd.endswith("/.ductor/workspace"):
        return cwd
    path = _extract_work_root("\n".join(text for text in texts if text))
    return path or cwd


def _extract_work_root(text: str) -> str:
    if not text:
        return ""
    candidates: list[tuple[str, bool, int]] = []
    patterns = (
        r"\bWork root(?: is)?\s+(`?)(/[^\s`,.;)]+)\1",
        r"\bWork in\s+(`?)(/[^\s`,.;)]+)\1",
        r"\bFrom\s+(`?)(/[^\s`,.;)]+)\1",
        r"\bin\s+(`?)(/[^\s`,.;)]+)\1",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            candidate = _semantic_candidate_path(match.group(2))
            if candidate:
                candidates.append((candidate, True, match.start()))
    for match in re.finditer(r"/[^\s`,;:)\\\]}]+", text):
        candidate = _semantic_candidate_path(match.group(0))
        if candidate:
            candidates.append((candidate, False, match.start()))
    return _choose_semantic_candidate(candidates)


def _semantic_candidate_path(raw: str) -> str:
    candidate = _clean_candidate_path(raw)
    if not candidate:
        return ""
    path = _nearest_attachable_path(Path(candidate).expanduser())
    if path == "" or _is_ductor_workspace_path(path) or _is_shallow_home_path(path):
        return ""
    return _project_root(path)


def _clean_candidate_path(raw: str) -> str:
    cleaned = raw.strip().rstrip("/:;,.)]}")
    if not cleaned.startswith("/"):
        return ""
    return str(Path(cleaned).expanduser())


def _nearest_attachable_path(raw: Path) -> str:
    if raw.is_dir() and _is_attachable_cwd(str(raw)):
        return str(raw)
    if raw.exists():
        parent = raw.parent
        if _is_attachable_cwd(str(parent)):
            return str(parent)
    return ""


def _project_root(raw: str) -> str:
    path = Path(raw)
    for candidate in (path, *path.parents):
        if (candidate / ".git").is_dir():
            return str(candidate)
    return str(path)


def _choose_semantic_candidate(candidates: list[tuple[str, bool, int]]) -> str:
    if not candidates:
        return ""
    best_score: tuple[int, int, int, int] | None = None
    best_path = ""
    seen: set[str] = set()
    for candidate, explicit, position in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        low_value = _is_low_value_semantic_root(candidate)
        score = (
            0 if low_value else 1,
            1 if explicit else 0,
            -len(Path(candidate).parts),
            -position,
        )
        if best_score is None or score > best_score:
            best_score = score
            best_path = candidate
    return best_path


def _is_low_value_semantic_root(raw: str) -> bool:
    return Path(raw).name == "Agents"


def _is_ductor_workspace_path(raw: str) -> bool:
    try:
        Path(raw).resolve().relative_to((Path.home() / ".ductor" / "workspace").resolve())
    except ValueError:
        return False
    return True


def _is_shallow_home_path(raw: str) -> bool:
    path = Path(raw)
    return path in {Path.home(), Path.home().parent}


def _extract_turn_context_model(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("model", "") or "").strip()


def _format_source(raw: object) -> str:
    formatted = ""
    if isinstance(raw, str):
        formatted = raw
    elif isinstance(raw, dict):
        subagent = raw.get("subagent")
        if not isinstance(subagent, dict):
            formatted = _clean_snippet(json.dumps(raw, sort_keys=True), limit=80)
        else:
            spawn = subagent.get("thread_spawn")
            if not isinstance(spawn, dict):
                formatted = "subagent"
            else:
                role = _clean_snippet(str(spawn.get("agent_role", "") or ""), limit=40)
                nickname = _clean_snippet(str(spawn.get("agent_nickname", "") or ""), limit=40)
                formatted = _format_subagent_source(role, nickname)
    return formatted


def _format_subagent_source(role: str, nickname: str) -> str:
    if role and nickname:
        return f"subagent:{role}:{nickname}"
    if role:
        return f"subagent:{role}"
    return "subagent"


def _merge_updated(item: dict[str, object], *, updated_at: str, updated_ts: float) -> tuple[str, float]:
    item_ts = _clean_timestamp(str(item.get("timestamp", "") or ""))
    item_ts_float = _parse_ts(item_ts)
    if item_ts_float >= updated_ts:
        return item_ts, item_ts_float
    return updated_at, updated_ts


def _parse_json_line(raw: str) -> dict[str, object] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return value


def _thread_updated_ts(updated_at: object, updated_at_ms: object) -> float:
    for raw, divisor in ((updated_at_ms, 1000.0), (updated_at, 1.0)):
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw) / divisor
        if isinstance(raw, str):
            try:
                value = float(raw)
            except ValueError:
                continue
            if value > 0:
                return value / divisor
    return 0.0


def _iter_tail_jsonl(path: Path, *, max_bytes: int = 131072) -> list[dict[str, object] | None]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            read_size = min(size, max_bytes)
            if read_size < size:
                handle.seek(-read_size, os.SEEK_END)
            else:
                handle.seek(0)
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    if read_size < size:
        newline = raw.find("\n")
        raw = raw[newline + 1 :] if newline >= 0 else ""
    return [_parse_json_line(line.strip()) for line in raw.splitlines() if line.strip()]


def _preferred_updated(
    index_row: tuple[str, str, float] | None,
    meta_row: _SessionMeta,
    history_row: _HistorySummary | None,
    state_row: _ThreadState | None,
) -> tuple[str, float]:
    candidates = [(meta_row.updated_ts, meta_row.updated_at)]
    if index_row is not None:
        _thread_name, index_updated_at, index_updated_ts = index_row
        candidates.append((index_updated_ts, index_updated_at))
    if history_row is not None:
        candidates.append((history_row.latest_ts, _format_ts(history_row.latest_ts)))
    if state_row is not None:
        candidates.append((state_row.updated_ts, state_row.updated_at))
    updated_ts, updated_at = max(candidates, key=lambda item: item[0])
    return updated_at, updated_ts


def _choose_thread_name(
    *,
    session_id: str,
    candidates: tuple[str, ...],
) -> str:
    for candidate in candidates:
        title = _clean_prompt_title(candidate)
        if title and not _is_unhelpful_thread_name(title, session_id):
            return title
    return session_id


def _is_unhelpful_thread_name(raw: str, session_id: str) -> bool:
    value = raw.strip()
    if not value:
        return True
    lower = value.casefold()
    session_lower = session_id.casefold()
    compact = re.sub(r"[^a-z0-9]", "", lower)
    looks_generated = bool(
        re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", lower)
        or re.fullmatch(r"rollout-\d{4}-\d{2}-\d{2}t[0-9a-z-]+", lower)
    )
    if len(compact) >= 24:
        digits = sum(ch.isdigit() for ch in compact)
        hexish = bool(re.fullmatch(r"[0-9a-f]+", compact))
        looks_generated = looks_generated or hexish or digits >= len(compact) // 2
    return (
        lower == session_lower
        or lower in session_lower
        or session_lower in lower
        or lower in {"untitled", "new chat", "new session"}
        or looks_generated
    )
