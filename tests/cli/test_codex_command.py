"""Tests for ``ductor codex`` helper commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ductor_bot.cli_commands import codex as codex_cmd
from ductor_bot.workspace.paths import DuctorPaths


def _paths(tmp_path: Path) -> DuctorPaths:
    paths = DuctorPaths(ductor_home=tmp_path)
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
                        "working_dir": "/tmp/pm99",
                        "source_kind": "codex_import",
                        "status": "idle",
                        "created_at": 1.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return paths


def test_codex_resume_prints_named_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(codex_cmd, "resolve_paths", lambda: _paths(tmp_path))

    codex_cmd.cmd_codex(["codex", "resume", "--print", "@pm99"])

    out = capsys.readouterr().out
    assert "codex resume --include-non-interactive --all --cd /tmp/pm99 named-session" in out


def test_codex_resume_launches_named_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[tuple[str, str]] = []
    monkeypatch.setattr(codex_cmd, "resolve_paths", lambda: _paths(tmp_path))
    monkeypatch.setattr(
        codex_cmd,
        "run_codex_resume",
        lambda session_id, working_dir: called.append((session_id, working_dir)) or 7,
    )

    with pytest.raises(SystemExit) as exc:
        codex_cmd.cmd_codex(["codex", "resume", "@pm99"])

    assert exc.value.code == 7
    assert called == [("named-session", "/tmp/pm99")]


def test_codex_resume_without_target_lists_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(codex_cmd, "resolve_paths", lambda: _paths(tmp_path))

    codex_cmd.cmd_codex(["codex", "resume"])

    out = capsys.readouterr().out
    assert "@pm99" in out
    assert "named-session" in out


def test_codex_help_shows_resume_command(capsys: pytest.CaptureFixture[str]) -> None:
    codex_cmd.cmd_codex(["codex", "--help"])

    out = capsys.readouterr().out
    assert "ductor codex resume" in out
    assert "Show resume examples and notes" in out


def test_codex_resume_help_explains_non_interactive_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    codex_cmd.cmd_codex(["codex", "resume", "--help"])

    out = capsys.readouterr().out
    assert "ductor codex resume @pm99" in out
    assert "codex exec" in out
