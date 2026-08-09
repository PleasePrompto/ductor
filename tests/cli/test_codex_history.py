"""Tests for browsing pre-existing Codex session history."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from ductor_bot.cli.codex_history import load_codex_history_browser


@pytest.fixture(autouse=True)
def ductor_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".ductor"
    home.mkdir()
    (home / "tasks.json").write_text('{"tasks": []}', encoding="utf-8")
    monkeypatch.setenv("DUCTOR_HOME", str(home))
    return home


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _write_state_db(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                title TEXT,
                first_user_message TEXT,
                updated_at INTEGER,
                updated_at_ms INTEGER,
                source TEXT,
                cli_version TEXT,
                model TEXT
            )
            """
        )
        for row in rows:
            con.execute(
                """
                INSERT INTO threads (
                    id, title, first_user_message, updated_at, updated_at_ms,
                    source, cli_version, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row.get("title", ""),
                    row.get("first_user_message", ""),
                    row.get("updated_at", 0),
                    row.get("updated_at_ms", 0),
                    row.get("source", ""),
                    row.get("cli_version", ""),
                    row.get("model", ""),
                ),
            )
        con.commit()
    finally:
        con.close()


def _write_ductor_tasks(home: Path, rows: list[dict[str, object]]) -> None:
    (home / "tasks.json").write_text(json.dumps({"tasks": rows}), encoding="utf-8")


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
    assert session.is_ductor_touched is False


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


def test_load_codex_history_browser_uses_current_state_titles_for_exec_sessions(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    project = tmp_path / "work" / "alpha"
    project.mkdir(parents=True)

    session_id = "019dd563-a84b-7883-bfe8-615fbfe55472"
    _write_state_db(
        codex_home / "state_5.sqlite",
        [
            {
                "id": session_id,
                "title": "Context: noisy setup. Task: create the isolated metadata335 candidate\n\n---\nTASK RULES",
                "first_user_message": "fallback prompt",
                "updated_at": 1_777_402_215,
                "source": "exec",
                "cli_version": "0.125.0",
                "model": "gpt-5.5",
            }
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "28" / "rollout.jsonl",
        [
            {
                "timestamp": "2026-04-28T18:39:35Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(project),
                    "source": "exec",
                    "cli_version": "0.125.0",
                },
            },
            {
                "timestamp": "2026-04-28T18:40:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "latest visible prompt"}],
                },
            },
            {
                "timestamp": "2026-04-28T18:50:15Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "candidate promoted"}],
                },
            },
        ],
    )

    browser = load_codex_history_browser(codex_home)

    session = browser.projects[0].sessions[0]
    assert session.thread_name == "create the isolated metadata335 candidate"
    assert session.first_prompt == "fallback prompt"
    assert session.preview == "latest visible prompt"
    assert session.updated_ts == 1_777_402_215
    assert session.model == "gpt-5.5"


def test_load_codex_history_browser_ignores_guid_index_title(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    project = tmp_path / "work" / "alpha"
    project.mkdir(parents=True)

    session_id = "019dd563-a84b-7883-bfe8-615fbfe55472"
    _write_jsonl(
        codex_home / "session_index.jsonl",
        [
            {
                "id": session_id,
                "thread_name": session_id,
                "updated_at": "2026-04-28T18:39:35Z",
            },
        ],
    )
    _write_state_db(
        codex_home / "state_5.sqlite",
        [
            {
                "id": session_id,
                "title": "Review the doctor chat logs and fix Codex ordering",
                "updated_at": 1_777_402_215,
            }
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "28" / "rollout.jsonl",
        [
            {
                "timestamp": "2026-04-28T18:39:35Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(project),
                    "source": "exec",
                    "cli_version": "0.125.0",
                },
            }
        ],
    )

    browser = load_codex_history_browser(codex_home)

    assert browser.projects[0].sessions[0].thread_name == "Review the doctor chat logs and fix Codex ordering"


def test_load_codex_history_browser_groups_ductor_worker_by_work_root(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    ductor_workspace = tmp_path / ".ductor" / "workspace"
    work_root = tmp_path / "pm99-research"
    ductor_workspace.mkdir(parents=True)
    work_root.mkdir()

    session_id = "019dd563-a84b-7883-bfe8-615fbfe55472"
    _write_state_db(
        codex_home / "state_5.sqlite",
        [
            {
                "id": session_id,
                "title": (
                    f"Context: Work root {work_root}. Combined baseline exists. "
                    "Task: validate the PM99 candidate"
                ),
                "updated_at": 1_777_402_215,
            }
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "28" / "rollout.jsonl",
        [
            {
                "timestamp": "2026-04-28T18:39:35Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(ductor_workspace),
                    "source": "exec",
                    "cli_version": "0.125.0",
                },
            }
        ],
    )

    browser = load_codex_history_browser(codex_home)

    assert browser.projects[0].working_dir == str(work_root)
    session = browser.projects[0].sessions[0]
    assert session.working_dir == str(work_root)
    assert session.launch_dir == str(ductor_workspace)
    assert session.thread_name == "validate the PM99 candidate"


def test_load_codex_history_browser_prefers_project_over_agents_workbench(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    ductor_workspace = tmp_path / ".ductor" / "workspace"
    agents_workbench = tmp_path / "Agents"
    work_root = tmp_path / "pm99-research"
    ductor_workspace.mkdir(parents=True)
    agents_workbench.mkdir()
    work_root.mkdir()

    session_id = "019dd563-a84b-7883-bfe8-615fbfe55472"
    _write_state_db(
        codex_home / "state_5.sqlite",
        [
            {
                "id": session_id,
                "title": (
                    f"From {agents_workbench} and {work_root}, inspect how to run a fresh "
                    "PM99 game root through the runner"
                ),
                "updated_at": 1_777_402_215,
            }
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "28" / "rollout.jsonl",
        [
            {
                "timestamp": "2026-04-28T18:39:35Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(ductor_workspace),
                    "source": "exec",
                    "cli_version": "0.125.0",
                },
            }
        ],
    )

    browser = load_codex_history_browser(codex_home)

    assert browser.projects[0].working_dir == str(work_root)


def test_load_codex_history_browser_uses_assistant_output_for_project_root(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    ductor_workspace = tmp_path / ".ductor" / "workspace"
    agents_workbench = tmp_path / "Agents"
    work_root = tmp_path / "pm99-research"
    ductor_workspace.mkdir(parents=True)
    agents_workbench.mkdir()
    work_root.mkdir()

    session_id = "019dd563-a84b-7883-bfe8-615fbfe55472"
    _write_state_db(
        codex_home / "state_5.sqlite",
        [
            {
                "id": session_id,
                "title": (
                    f"From {agents_workbench}, inspect the repo/workspace for football kit "
                    "bitmap assets and tooling"
                ),
                "updated_at": 1_777_402_215,
            }
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "28" / "rollout.jsonl",
        [
            {
                "timestamp": "2026-04-28T18:39:35Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(ductor_workspace),
                    "source": "exec",
                    "cli_version": "0.125.0",
                },
            },
            {
                "timestamp": "2026-04-28T18:50:15Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                f"Scope note: {agents_workbench} itself does not contain the "
                                f"PM99 assets. The relevant workspace is {work_root}."
                            ),
                        }
                    ],
                },
            },
        ],
    )

    browser = load_codex_history_browser(codex_home)

    assert browser.projects[0].working_dir == str(work_root)


def test_load_codex_history_browser_uses_ductor_task_name_for_worker(
    tmp_path: Path,
    ductor_home: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    ductor_workspace = tmp_path / ".ductor" / "workspace"
    work_root = tmp_path / "pm99-research"
    ductor_workspace.mkdir(parents=True, exist_ok=True)
    work_root.mkdir()
    (work_root / ".git").mkdir()
    (work_root / "work" / "pm99" / "joe" / "run").mkdir(parents=True)

    session_id = "019dd563-a84b-7883-bfe8-615fbfe55472"
    _write_ductor_tasks(
        ductor_home,
        [
            {
                "task_id": "22e7a8b5",
                "name": "PM99 metadata335 isolated apply runner smoke",
                "session_id": session_id,
                "prompt_preview": "Context: generated worker prompt",
                "result_preview": f"Run Root `{work_root}/work/pm99/joe/run`",
            }
        ],
    )
    _write_state_db(
        codex_home / "state_5.sqlite",
        [
            {
                "id": session_id,
                "title": (
                    f"create a fresh isolated copy of the combined baseline under {work_root}/work"
                ),
                "updated_at": 1_777_402_215,
            }
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "28" / "rollout.jsonl",
        [
            {
                "timestamp": "2026-04-28T18:39:35Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(ductor_workspace),
                    "source": "exec",
                    "cli_version": "0.125.0",
                },
            }
        ],
    )

    browser = load_codex_history_browser(codex_home)
    session = browser.projects[0].sessions[0]

    assert session.thread_name == "PM99 metadata335 isolated apply runner smoke"
    assert session.source == "task:22e7a8b5"
    assert session.working_dir == str(work_root)
    assert session.is_ductor_touched is True
    assert session.is_ductor_task is True


def test_load_codex_history_browser_uses_historical_workstream_root_for_workers(
    tmp_path: Path,
    ductor_home: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    ductor_workspace = tmp_path / ".ductor" / "workspace"
    work_root = tmp_path / "pm99-research"
    log_dir = ductor_home / "logs"
    ductor_workspace.mkdir(parents=True, exist_ok=True)
    work_root.mkdir()
    (work_root / ".git").mkdir()
    log_dir.mkdir()

    session_id = "019dd563-a84b-7883-bfe8-615fbfe55472"
    _write_ductor_tasks(
        ductor_home,
        [
            {
                "task_id": "22e7a8b5",
                "name": "PM99 metadata335 isolated apply runner smoke",
                "session_id": session_id,
                "prompt_preview": f"Context: We are progressing PM99 modern English alias-safe baseline. Work root {work_root}",
                "result_preview": f"Run Root `{work_root}/work/pm99/joe/run`",
            }
        ],
    )
    (log_dir / "agent.log").write_text(
        "2026-04-28 13:52:05 [INFO] ductor_bot.orchestrator.core:core.py:302: "
        "[main:msg:1430915682] Message received text=review how kit bitmaps work, "
        "how confident do you feel going and updating stoke city\n"
        "2026-04-28 13:52:21 [INFO] ductor_bot.tasks.registry:registry.py:132: "
        "Task created id=a99190bd name='Kit bitmap workflow review' provider=\n"
        "2026-04-28 18:26:33 [INFO] ductor_bot.orchestrator.core:core.py:302: "
        "[main:msg:1430915682] Message received text=can you pick up the "
        "alias-safe baseline next then\n"
        "2026-04-28 19:39:31 [INFO] ductor_bot.tasks.registry:registry.py:132: "
        "Task created id=22e7a8b5 name='PM99 metadata335 isolated apply runner smoke' provider=\n",
        encoding="utf-8",
    )
    _write_state_db(
        codex_home / "state_5.sqlite",
        [
            {
                "id": session_id,
                "title": f"Context: generated worker title. Work root {work_root}. Task: create a fresh isolated copy",
                "updated_at": 1_777_402_215,
            }
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "28" / "rollout.jsonl",
        [
            {
                "timestamp": "2026-04-28T18:39:35Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(ductor_workspace),
                    "source": "exec",
                    "cli_version": "0.125.0",
                },
            }
        ],
    )

    browser = load_codex_history_browser(codex_home)
    session = browser.projects[0].sessions[0]

    assert session.thread_name == (
        "PM99 metadata335 isolated apply runner smoke / "
        "review how kit bitmaps work, how confident do you feel going and updating stoke city"
    )
    assert "alias-safe baseline next then" not in session.thread_name
    assert session.is_ductor_touched is True
    assert session.is_ductor_task is True


def test_load_codex_history_browser_marks_latest_ductor_prompt_from_logs(
    tmp_path: Path,
    ductor_home: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    project = tmp_path / "work" / "alpha"
    log_dir = ductor_home / "logs"
    project.mkdir(parents=True)
    log_dir.mkdir()

    session_id = "019dd563-a84b-7883-bfe8-615fbfe55472"
    _write_jsonl(
        codex_home / "history.jsonl",
        [
            {"session_id": session_id, "ts": 0.0, "text": "resume this from ductor"},
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "29" / "rollout.jsonl",
        [
            {
                "timestamp": "2026-04-29T00:00:06Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(project),
                    "source": "cli",
                    "cli_version": "1.2.3",
                },
            }
        ],
    )
    (log_dir / "agent.log").write_text(
        "2026-04-29 00:00:05 [INFO] ductor_bot.cli.codex_provider:codex_provider.py:293: "
        "[main:msg:1430915682:019dd563] Codex stream cmd: "
        "/home/joe/.npm-global/bin/codex exec resume --json "
        "--dangerously-bypass-approvals-and-sandbox -- "
        f"{session_id} Resume this from ductor\n",
        encoding="utf-8",
    )

    browser = load_codex_history_browser(codex_home)

    assert browser.projects[0].sessions[0].is_ductor_touched is True


def test_load_codex_history_browser_does_not_mark_when_desktop_prompt_is_newer(
    tmp_path: Path,
    ductor_home: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    project = tmp_path / "work" / "alpha"
    log_dir = ductor_home / "logs"
    project.mkdir(parents=True)
    log_dir.mkdir()

    session_id = "019dd563-a84b-7883-bfe8-615fbfe55472"
    _write_jsonl(
        codex_home / "history.jsonl",
        [
            {"session_id": session_id, "ts": 99_999_999_999.0, "text": "newer desktop prompt"},
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "29" / "rollout.jsonl",
        [
            {
                "timestamp": "2026-04-29T01:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(project),
                    "source": "cli",
                    "cli_version": "1.2.3",
                },
            }
        ],
    )
    (log_dir / "agent.log").write_text(
        "2026-04-29 00:00:05 [INFO] ductor_bot.cli.codex_provider:codex_provider.py:293: "
        "[main:msg:1430915682:019dd563] Codex stream cmd: "
        "/home/joe/.npm-global/bin/codex exec resume --json "
        "--dangerously-bypass-approvals-and-sandbox -- "
        f"{session_id} Older ductor prompt\n",
        encoding="utf-8",
    )

    browser = load_codex_history_browser(codex_home)

    assert browser.projects[0].sessions[0].is_ductor_touched is False


def test_load_codex_history_browser_sorts_human_sessions_before_workers(
    tmp_path: Path,
    ductor_home: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    ductor_workspace = tmp_path / ".ductor" / "workspace"
    work_root = tmp_path / "pm99-research"
    ductor_workspace.mkdir(parents=True)
    work_root.mkdir()
    (work_root / ".git").mkdir()

    _write_ductor_tasks(
        ductor_home,
        [
            {
                "task_id": "22e7a8b5",
                "name": "PM99 metadata335 isolated apply runner smoke",
                "session_id": "task-session",
                "prompt_preview": f"Work root {work_root}. Task: generated worker",
                "result_preview": "",
            }
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "29" / "desktop.jsonl",
        [
            {
                "timestamp": "2026-04-29T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "desktop-session",
                    "cwd": str(work_root),
                    "source": "cli",
                    "cli_version": "1.2.3",
                },
            }
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "29" / "task.jsonl",
        [
            {
                "timestamp": "2026-04-29T01:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "task-session",
                    "cwd": str(ductor_workspace),
                    "source": "exec",
                    "cli_version": "1.2.3",
                },
            }
        ],
    )
    _write_jsonl(
        codex_home / "sessions" / "2026" / "04" / "29" / "agent.jsonl",
        [
            {
                "timestamp": "2026-04-29T02:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "agent-session",
                    "cwd": str(work_root),
                    "source": {"subagent": {"thread_spawn": {"agent_role": "explorer"}}},
                    "cli_version": "1.2.3",
                },
            }
        ],
    )

    browser = load_codex_history_browser(codex_home)
    sessions = browser.projects[0].sessions

    assert [session.session_id for session in sessions] == [
        "desktop-session",
        "agent-session",
        "task-session",
    ]
    assert browser.projects[0].updated_ts == sessions[1].updated_ts
