"""Read resumable Codex session metadata from the local Codex home."""

from __future__ import annotations

import html
import json
import os
import re
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


@dataclass(frozen=True, slots=True)
class _HistorySummary:
    """Prompt history derived from Codex history.jsonl."""

    first_prompt: str
    latest_prompt: str
    prompt_count: int


def codex_home() -> Path:
    """Return the effective Codex home directory."""
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def load_codex_history_browser(home: Path | None = None) -> CodexHistoryBrowser:
    """Load recent Codex sessions grouped by recorded working directory."""
    codex_dir = home or codex_home()
    index_path = codex_dir / "session_index.jsonl"
    history_path = codex_dir / "history.jsonl"
    sessions_dir = codex_dir / "sessions"
    history_available = index_path.exists() or history_path.exists() or sessions_dir.exists()
    if not sessions_dir.exists():
        return CodexHistoryBrowser(projects=(), skipped_count=0, history_available=history_available)

    index_rows = _read_session_index(index_path) if index_path.exists() else {}
    history = _read_history_summaries(history_path) if history_path.exists() else {}
    meta = _read_session_meta(sessions_dir)

    grouped: dict[str, list[CodexHistorySession]] = {}
    skipped_count = 0
    for session_id, meta_row in meta.items():
        index_row = index_rows.get(session_id)
        history_row = history.get(session_id)
        preview = history_row.latest_prompt if history_row is not None else ""
        first_prompt = history_row.first_prompt if history_row is not None else ""
        turn_count = history_row.prompt_count if history_row is not None else 0
        thread_name = (
            index_row[0]
            if index_row is not None
            else first_prompt
            or preview
            or session_id
        )
        updated_at, updated_ts = _preferred_updated(index_row, meta_row)
        working_dir = meta_row.working_dir
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
            source=meta_row.source,
            cli_version=meta_row.cli_version,
            summary=summary,
            first_prompt=first_prompt,
            last_reply=meta_row.last_reply,
            last_output_summary=_summarize_output(meta_row.last_reply),
            turn_count=turn_count,
            model=meta_row.model,
        )
        grouped.setdefault(working_dir, []).append(session)

    projects: list[CodexHistoryProject] = []
    for working_dir, sessions in grouped.items():
        ordered = tuple(sorted(sessions, key=lambda item: (-item.updated_ts, item.session_id)))
        latest = ordered[0]
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
        )
        for session_id, (_first_ts, first_prompt, _latest_ts, latest_prompt, count) in by_session.items()
    }


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
            updated_at, updated_ts, last_reply, model = _scan_session_events(
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
    source = str(payload.get("source", "") or "")
    cli_version = str(payload.get("cli_version", "") or "")
    updated_at = _clean_timestamp(str(row.get("timestamp", "") or ""))
    return session_id, working_dir, source, cli_version, updated_at, _parse_ts(updated_at)


def _scan_session_events(
    session_file: Path,
    *,
    initial_updated_at: str,
    initial_updated_ts: float,
) -> tuple[str, float, str, str]:
    updated_at = initial_updated_at
    updated_ts = initial_updated_ts
    last_reply = ""
    model = ""
    for item in _iter_tail_jsonl(session_file):
        if item is None:
            continue
        updated_at, updated_ts = _merge_updated(item, updated_at=updated_at, updated_ts=updated_ts)
        item_type = str(item.get("type", "") or "")
        if item_type == "event_msg":
            message = _extract_event_agent_message(item.get("payload"))
            if message:
                last_reply = message
            continue
        if item_type == "response_item":
            message = _extract_assistant_output(item.get("payload"))
            if message:
                last_reply = message
            continue
        if item_type == "turn_context":
            model_value = _extract_turn_context_model(item.get("payload"))
            if model_value:
                model = model_value
    return updated_at, updated_ts, last_reply, model


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
        text = _clean_snippet(str(item.get("text", "") or ""), limit=240)
        if text:
            chunks.append(text)
    if not chunks:
        return ""
    return " ".join(chunks)[:240]


def _extract_event_agent_message(payload: object) -> str:
    if not isinstance(payload, dict) or payload.get("type") != "agent_message":
        return ""
    return _clean_snippet(str(payload.get("message", "") or ""), limit=240)


def _extract_turn_context_model(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("model", "") or "").strip()


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
) -> tuple[str, float]:
    if index_row is None:
        return meta_row.updated_at, meta_row.updated_ts
    _thread_name, index_updated_at, index_updated_ts = index_row
    if meta_row.updated_ts >= index_updated_ts:
        return meta_row.updated_at, meta_row.updated_ts
    return index_updated_at, index_updated_ts
