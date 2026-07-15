"""Stop hook helpers for interactive Claude REPL turns."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"\[\[reply-token:(\w+)\]\]")


def _content_to_text(value: object) -> str:
    """Extract human-readable text from Claude transcript content."""
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
    """Return the effective transcript event type."""
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
    """Return transcript text from common Claude JSONL shapes."""
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


def iter_jsonl(path: Path, *, start_offset: int = 0) -> Iterable[dict[str, Any]]:
    """Yield JSON objects from a transcript JSONL file."""
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
    """Return whether a user event carries tool_result content."""
    message = event.get("message")
    content = message.get("content") if isinstance(message, Mapping) else event.get("content")
    if isinstance(content, Iterable) and not isinstance(content, (str, bytes)):
        return any(isinstance(b, Mapping) and b.get("type") == "tool_result" for b in content)
    return False


def extract_last_user_nonce(transcript_path: Path) -> str | None:
    """Return the last reply-token nonce carried by a user event."""
    last_nonce: str | None = None
    for event in iter_jsonl(transcript_path):
        if _event_type(event) != "user":
            continue
        if _is_tool_result_event(event):
            continue
        match = TOKEN_RE.search(_event_text(event))
        if match:
            last_nonce = match.group(1)
    return last_nonce


def signal_file_path(signal_dir: Path, agent: str, session_id: str, nonce: str) -> Path:
    """Return the canonical Stop-hook signal path."""
    return signal_dir / agent / f"{session_id}.{nonce}.done"


def touch_signal(signal_dir: Path, agent: str, session_id: str, nonce: str) -> Path:
    """Create a 0600 completion signal file."""
    path = signal_file_path(signal_dir, agent, session_id, nonce)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    path.chmod(0o600)
    return path


def handle_stop_payload(payload: Mapping[str, Any], *, signal_dir: Path, agent: str) -> Path | None:
    """Touch the per-turn signal file when the Stop hook payload has a nonce."""
    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(transcript_path, str) or not transcript_path:
        return None

    nonce = extract_last_user_nonce(Path(transcript_path))
    if nonce is None:
        return None
    return touch_signal(signal_dir, agent, session_id, nonce)


def merge_stop_hook_settings(settings_path: Path, *, command: str, backup: bool = True) -> None:
    """Merge a Claude Stop hook command into settings.json, preserving existing hooks."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        data = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
        if backup:
            shutil.copy2(settings_path, settings_path.with_suffix(settings_path.suffix + ".bak"))
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    stop_hooks = hooks.setdefault("Stop", [])
    if not isinstance(stop_hooks, list):
        stop_hooks = []
        hooks["Stop"] = stop_hooks

    command_hook = {"type": "command", "command": command}
    for entry in stop_hooks:
        if not isinstance(entry, dict):
            continue
        entry_hooks = entry.get("hooks")
        if isinstance(entry_hooks, list) and command_hook in entry_hooks:
            settings_path.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return

    stop_hooks.append({"matcher": "*", "hooks": [command_hook]})
    settings_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for Claude Stop hook execution."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-dir", required=True)
    parser.add_argument("--agent", required=True)
    args = parser.parse_args(argv)

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if isinstance(payload, dict):
        handle_stop_payload(payload, signal_dir=Path(args.signal_dir), agent=args.agent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
