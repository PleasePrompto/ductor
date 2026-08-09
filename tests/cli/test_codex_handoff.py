"""Tests for Codex desktop handoff helpers."""

from __future__ import annotations

import json
from pathlib import Path

from ductor_bot.cli.codex_handoff import (
    build_codex_resume_args,
    build_codex_resume_command,
    find_resume_target,
    latest_resume_target,
    load_resume_targets,
)
from ductor_bot.workspace.paths import DuctorPaths


def _paths(tmp_path: Path) -> DuctorPaths:
    return DuctorPaths(ductor_home=tmp_path)


def test_build_codex_resume_command_quotes_working_dir() -> None:
    command = build_codex_resume_command("sess-1", "/tmp/project with spaces")

    assert command == (
        "codex resume --include-non-interactive --all --cd "
        "'/tmp/project with spaces' sess-1"
    )
    assert build_codex_resume_args("sess-1", "/tmp/project with spaces") == [
        "codex",
        "resume",
        "--include-non-interactive",
        "--all",
        "--cd",
        "/tmp/project with spaces",
        "sess-1",
    ]


def test_load_resume_targets_reads_main_and_named_sessions(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.sessions_path.write_text(
        json.dumps(
            {
                "tg:1": {
                    "transport": "tg",
                    "chat_id": 1,
                    "topic_id": None,
                    "provider": "codex",
                    "model": "gpt-5.4",
                    "last_active": "2026-04-24T10:00:00+00:00",
                    "provider_sessions": {
                        "codex": {
                            "session_id": "main-session",
                            "working_dir": "/tmp/main",
                            "source_kind": "codex_import",
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    paths.named_sessions_path.write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "name": "pm99",
                        "chat_id": 1,
                        "provider": "codex",
                        "model": "gpt-5.4",
                        "session_id": "named-session",
                        "working_dir": "/tmp/named",
                        "source_kind": "codex_import",
                        "status": "idle",
                        "created_at": 1.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    targets = load_resume_targets(paths)

    assert [target.target for target in targets] == ["main:tg:1", "@pm99"]
    assert targets[0].session_id == "main-session"
    assert find_resume_target(paths, "@pm99").session_id == "named-session"  # type: ignore[union-attr]
    assert find_resume_target(paths, "pm99").session_id == "named-session"  # type: ignore[union-attr]
    assert latest_resume_target(paths, main_only=True).session_id == "main-session"  # type: ignore[union-attr]


def test_load_resume_targets_falls_back_for_ductor_created_sessions(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.sessions_path.write_text(
        json.dumps(
            {
                "tg:1": {
                    "transport": "tg",
                    "chat_id": 1,
                    "provider": "codex",
                    "model": "gpt-5.4",
                    "last_active": "2026-04-24T10:00:00+00:00",
                    "provider_sessions": {
                        "codex": {
                            "session_id": "main-session",
                            "working_dir": "",
                            "source_kind": "ductor",
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    paths.named_sessions_path.write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "name": "pm99",
                        "chat_id": 1,
                        "provider": "codex",
                        "model": "gpt-5.4",
                        "session_id": "named-session",
                        "working_dir": "",
                        "source_kind": "ductor",
                        "status": "idle",
                        "created_at": 1.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    targets = load_resume_targets(paths)

    assert [target.working_dir for target in targets] == [str(paths.workspace), str(paths.workspace)]


def test_load_resume_targets_skips_unresumable_entries(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.sessions_path.write_text(
        json.dumps({"tg:1": {"chat_id": 1, "provider_sessions": {"codex": {"session_id": ""}}}}),
        encoding="utf-8",
    )
    paths.named_sessions_path.write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "name": "ended",
                        "chat_id": 1,
                        "provider": "codex",
                        "session_id": "ended-session",
                        "status": "ended",
                    },
                    {"name": "claude", "chat_id": 1, "provider": "claude", "session_id": "x"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert load_resume_targets(paths) == []
