"""Tests for browsing pre-existing Codex session history."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ductor_bot.cli.codex_history import load_codex_history_browser


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_load_codex_history_browser_groups_attachable_sessions(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    attachable = tmp_path / "work" / "alpha"
    attachable.mkdir(parents=True)

    _write_jsonl(
        codex_home / "session_index.jsonl",
        [
            {
                "id": "sess-1",
                "thread_name": "Alpha&#x20;Thread",
                "updated_at": "2026-04-08T12:00:00Z",
            },
            {
                "id": "sess-2",
                "thread_name": "Missing",
                "updated_at": "2026-04-07T12:00:00Z",
            },
        ],
    )
    _write_jsonl(
        codex_home / "history.jsonl",
        [
            {"session_id": "sess-1", "ts": 10, "text": "older prompt"},
            {"session_id": "sess-1", "ts": 20, "text": "latest prompt preview"},
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "08" / "sess-1.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "sess-1",
                    "cwd": str(attachable),
                    "source": "vscode",
                    "cli_version": "1.2.3",
                },
            },
            {
                "timestamp": "2026-04-08T12:05:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.4"},
            },
            {
                "timestamp": "2026-04-08T12:06:00Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "latest assistant reply"},
            },
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "07" / "sess-2.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "sess-2",
                    "cwd": str(tmp_path / "missing"),
                    "source": "terminal",
                    "cli_version": "1.0.0",
                },
            }
        ],
    )

    browser = load_codex_history_browser(codex_home)

    assert browser.history_available is True
    assert browser.skipped_count == 1
    assert len(browser.projects) == 1
    project = browser.projects[0]
    assert project.working_dir == str(attachable)
    assert project.label == "alpha"
    assert len(project.sessions) == 1

    session = project.sessions[0]
    assert session.session_id == "sess-1"
    assert session.thread_name == "Alpha Thread"
    assert session.preview == "latest prompt preview"
    assert session.source == "vscode"
    assert session.cli_version == "1.2.3"
    assert session.summary == "Alpha Thread | latest prompt preview"
    assert session.first_prompt == "older prompt"
    assert session.turn_count == 2
    assert session.last_reply == "latest assistant reply"
    assert session.last_output_summary == "latest assistant reply"
    assert session.model == "gpt-5.4"


def test_load_codex_history_browser_uses_session_files_when_index_is_stale(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    root_project = tmp_path / "work" / "pm99-research"
    root_project.mkdir(parents=True)

    _write_jsonl(
        codex_home / "session_index.jsonl",
        [
            {
                "id": "sess-old",
                "thread_name": "Old title",
                "updated_at": "2026-03-01T12:00:00Z",
            }
        ],
    )
    _write_jsonl(
        codex_home / "history.jsonl",
        [
            {"session_id": "sess-old", "ts": 10, "text": "fresh preview from history"},
            {"session_id": "sess-new", "ts": 20, "text": "brand new preview from history"},
        ],
    )

    stale_file = codex_home / "sessions" / "2026" / "03" / "01" / "sess-old.jsonl"
    _write_jsonl(
        stale_file,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "sess-old",
                    "cwd": str(root_project),
                    "source": "cli",
                    "cli_version": "1.2.3",
                },
            }
        ],
    )
    fresh_file = codex_home / "sessions" / "2026" / "04" / "10" / "sess-new.jsonl"
    _write_jsonl(
        fresh_file,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "sess-new",
                    "cwd": str(root_project),
                    "source": "cli",
                    "cli_version": "1.2.4",
                },
            },
            {
                "timestamp": "2026-04-10T09:00:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.4"},
            },
            {
                "timestamp": "2026-04-10T09:01:00Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "latest assistant note"},
            },
        ],
    )

    os.utime(stale_file, (1_775_000_000, 1_775_000_000))
    os.utime(fresh_file, (1_776_000_000, 1_776_000_000))

    browser = load_codex_history_browser(codex_home)

    assert browser.history_available is True
    assert browser.skipped_count == 0
    assert len(browser.projects) == 1

    project = browser.projects[0]
    assert project.working_dir == str(root_project)
    assert project.sessions[0].session_id == "sess-new"
    assert project.sessions[0].thread_name == "brand new preview from history"
    assert project.sessions[0].first_prompt == "brand new preview from history"
    assert project.sessions[0].last_reply == "latest assistant note"
    assert project.sessions[0].last_output_summary == "latest assistant note"
    assert project.sessions[0].model == "gpt-5.4"
    assert project.sessions[1].session_id == "sess-old"
    assert project.sessions[1].preview == "fresh preview from history"
    assert project.updated_ts == project.sessions[0].updated_ts


def test_load_codex_history_browser_loads_without_index_file(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    project = tmp_path / "work" / "alpha"
    project.mkdir(parents=True)

    _write_jsonl(
        codex_home / "history.jsonl",
        [
            {"session_id": "sess-only", "ts": 1, "text": "hello from history"},
        ],
    )
    session_file = codex_home / "sessions" / "2026" / "04" / "10" / "sess-only.jsonl"
    _write_jsonl(
        session_file,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "sess-only",
                    "cwd": str(project),
                    "source": "cli",
                    "cli_version": "1.2.3",
                },
            },
            {
                "timestamp": "2026-04-10T12:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "assistant output"}],
                },
            },
        ],
    )

    browser = load_codex_history_browser(codex_home)

    assert browser.history_available is True
    assert len(browser.projects) == 1
    assert browser.projects[0].sessions[0].session_id == "sess-only"
    assert browser.projects[0].sessions[0].thread_name == "hello from history"
    assert browser.projects[0].sessions[0].last_reply == "assistant output"
    assert browser.projects[0].sessions[0].last_output_summary == "assistant output"
