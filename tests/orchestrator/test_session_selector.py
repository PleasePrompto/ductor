"""Tests for the Telegram session selector including Codex imports."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.cli.codex_history import (
    CodexHistoryBrowser,
    CodexHistoryProject,
    CodexHistorySession,
)
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.selectors.session_selector import (
    handle_session_callback,
    session_selector_start,
)
from ductor_bot.session.key import SessionKey


def _browser(
    workdir: str,
    *,
    launch_dir: str = "",
    is_ductor_touched: bool = False,
    is_ductor_task: bool = False,
    is_subagent: bool = False,
) -> CodexHistoryBrowser:
    session = CodexHistorySession(
        session_id="sess-import-1",
        thread_name="Imported thread",
        updated_at="2026-04-08T12:00:00Z",
        updated_ts=1_744_113_600.0,
        working_dir=workdir,
        preview="latest imported prompt",
        source="terminal",
        cli_version="1.2.3",
        summary="Imported thread | latest imported prompt",
        first_prompt="first imported prompt",
        last_reply="last assistant reply",
        last_output_summary="assistant summary",
        turn_count=4,
        model="gpt-5.4",
        launch_dir=launch_dir,
        is_ductor_touched=is_ductor_touched,
        is_ductor_task=is_ductor_task,
        is_subagent=is_subagent,
    )
    project = CodexHistoryProject(
        working_dir=workdir,
        label=Path(workdir).name,
        updated_at=session.updated_at,
        updated_ts=session.updated_ts,
        sessions=(session,),
    )
    return CodexHistoryBrowser(projects=(project,), skipped_count=0, history_available=True)


def _history_session(
    *,
    session_id: str,
    thread_name: str,
    workdir: str,
    updated_ts: float,
    is_ductor_task: bool = False,
) -> CodexHistorySession:
    return CodexHistorySession(
        session_id=session_id,
        thread_name=thread_name,
        updated_at="2026-04-08T12:00:00Z",
        updated_ts=updated_ts,
        working_dir=workdir,
        preview=f"latest prompt for {thread_name}",
        source="terminal",
        cli_version="1.2.3",
        summary=thread_name,
        first_prompt=f"first prompt for {thread_name}",
        turn_count=4,
        model="gpt-5.4",
        is_ductor_touched=is_ductor_task,
        is_ductor_task=is_ductor_task,
    )


def _mixed_browser(workdir: str) -> CodexHistoryBrowser:
    sessions = (
        *(
            _history_session(
                session_id=f"human-{idx}",
                thread_name=f"Human desktop session {idx}",
                workdir=workdir,
                updated_ts=1_744_113_700.0 - idx,
            )
            for idx in range(6)
        ),
        _history_session(
            session_id="task-1",
            thread_name="Review how kit bitmaps work",
            workdir=workdir,
            updated_ts=1_744_113_600.0,
            is_ductor_task=True,
        ),
        _history_session(
            session_id="task-2",
            thread_name="PM99 metadata335 isolated apply runner smoke",
            workdir=workdir,
            updated_ts=1_744_113_500.0,
            is_ductor_task=True,
        ),
    )
    project = CodexHistoryProject(
        working_dir=workdir,
        label=Path(workdir).name,
        updated_at=sessions[0].updated_at,
        updated_ts=sessions[0].updated_ts,
        sessions=sessions,
    )
    return CodexHistoryBrowser(projects=(project,), skipped_count=0, history_available=True)


async def test_root_page_shows_browse_codex_button(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )

    resp = await session_selector_start(orch, SessionKey(chat_id=1))

    assert resp.buttons is not None
    labels = [button.text for row in resp.buttons.rows for button in row]
    assert "Browse Codex" in labels


async def test_root_page_shows_readable_active_target_and_switches_to_main(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: CodexHistoryBrowser(projects=()),
    )
    key = SessionKey(chat_id=1, topic_id=44)
    ns = orch._named_sessions.create(1, "codex", "gpt-5.4", "Prepare the July release notes", key=key)
    orch._named_sessions.update_after_response(1, ns.name, "sid")

    selected = await handle_session_callback(orch, key, f"nsc:sw:{ns.name}")
    assert "Current target:** Prepare the July release notes" in selected.text
    assert "Use main chat" in [button.text for row in selected.buttons.rows for button in row]

    main = await handle_session_callback(orch, key, "nsc:swm")
    assert "Current target:** Main chat" in main.text


async def test_selector_rename_prompts_for_next_message(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: CodexHistoryBrowser(projects=()),
    )
    key = SessionKey(chat_id=1)
    ns = orch._named_sessions.create(1, "codex", "gpt-5.4", "Prepare release notes", key=key)

    response = await handle_session_callback(orch, key, f"nsc:ren:{ns.name}")

    assert "Send the new title" in response.text
    assert orch._named_sessions.pending_rename(key) == ns.name


async def test_project_page_shows_start_fresh_button(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), "nsc:cxs:0:0")

    assert resp.buttons is not None
    labels = [button.text for row in resp.buttons.rows for button in row]
    assert "Start Fresh Here" in labels
    assert "gpt-5.4" in resp.text
    assert "first imported prompt" in resp.text
    assert "latest imported prompt" in resp.text
    assert "assistant summary" in resp.text


async def test_codex_sessions_page_surfaces_task_rows_when_hidden_by_human_sort(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _mixed_browser(str(orch.paths.workspace)),
    )

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), "nsc:cxs:0:0")

    assert "H:6 T:2 A:0" in resp.text
    assert "D=Ductor T=Task A=Agent" in resp.text
    assert "Recent background tasks:" in resp.text
    assert "Task 1. (Task) Review how kit bitmaps work" in resp.text
    assert "Task 2. (Task) PM99 metadata335 isolated apply runner smoke" in resp.text
    assert "first prompt for Review how kit bitmaps work" not in resp.text
    assert resp.buttons is not None
    labels = [button.text for row in resp.buttons.rows for button in row]
    assert any(label.startswith("Task 1. (Task)") for label in labels)
    assert any(label.startswith("Task 2. (Task)") for label in labels)


async def test_codex_sessions_page_marks_ductor_touched_sessions(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace), is_ductor_touched=True),
    )

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), "nsc:cxs:0:0")

    assert "(D) Imported thread" in resp.text
    assert resp.buttons is not None
    labels = [button.text for row in resp.buttons.rows for button in row]
    assert any("(D) Imported thread" in label for label in labels)


async def test_codex_sessions_page_marks_task_sessions_separately(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(
            str(orch.paths.workspace),
            is_ductor_touched=True,
            is_ductor_task=True,
        ),
    )

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), "nsc:cxs:0:0")

    assert "(Task) Imported thread" in resp.text
    assert "(D) Imported thread" not in resp.text


async def test_attach_codex_import_updates_current_chat_provider_bucket(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )

    key = SessionKey(chat_id=1, topic_id=77)
    resp = await handle_session_callback(orch, key, "nsc:cxu:0:0:0:0")

    assert "Attached Codex session" in resp.text
    active = await orch._sessions.get_active(key)
    assert active is not None
    assert active.provider == "codex"
    assert active.session_id == "sess-import-1"
    assert active.working_dir == str(orch.paths.workspace)
    assert active.source_kind == "codex_import"


async def test_root_page_shows_current_chat_and_attached_markers(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )
    key = SessionKey(chat_id=1)
    await orch.attach_codex_import(
        key,
        session_id="sess-import-1",
        working_dir=str(orch.paths.workspace),
    )
    await orch.set_main_planner_state(
        key,
        provider="codex",
        model=orch.codex_import_model(),
        enabled=True,
        waiting=True,
    )

    resp = await session_selector_start(orch, key)

    assert "Current chat:" in resp.text
    assert "attached" in resp.text
    assert "plan:on" in resp.text
    assert "awaiting reply" in resp.text
    assert resp.buttons is not None
    labels = [button.text for row in resp.buttons.rows for button in row]
    assert "Desktop Resume Command" in labels


async def test_root_page_shows_named_attached_planner_markers(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )
    session = orch.import_codex_named_session(
        1,
        session_id="sess-import-1",
        working_dir=str(orch.paths.workspace),
        thread_name="planner",
        prompt_preview="previous prompt",
    )
    orch.named_sessions.set_planner_mode(1, session.name, True)
    orch.named_sessions.mark_running(1, session.name, "plan this")

    resp = await session_selector_start(orch, SessionKey(chat_id=1))

    assert f"**{session.name}**" in resp.text
    assert "attached" in resp.text
    assert "plan:on" in resp.text
    assert "awaiting reply" in resp.text
    assert resp.buttons is not None
    labels = [button.text for row in resp.buttons.rows for button in row]
    assert "Desktop Resume Command" in labels


async def test_attach_codex_import_shows_confirmation_when_codex_bucket_exists(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )

    key = SessionKey(chat_id=1)
    await orch._sessions.set_provider_session_state(
        key,
        provider="codex",
        model="gpt-5.2-codex",
        session_id="existing-codex",
        working_dir=str(orch.paths.workspace),
        source_kind="ductor",
    )

    resp = await handle_session_callback(orch, key, "nsc:cxu:0:0:0:0")

    assert "Replace Current Codex Context" in resp.text


async def test_attach_codex_import_is_blocked_in_docker_mode(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )
    orch._config.docker.enabled = True

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), "nsc:cxu:0:0:0:0")

    assert "host mode" in resp.text


async def test_start_fresh_codex_session_updates_current_chat_provider_bucket(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )

    key = SessionKey(chat_id=1, topic_id=77)
    resp = await handle_session_callback(orch, key, "nsc:cxf:0:0")

    assert "Started a fresh Codex session" in resp.text
    active = await orch._sessions.get_active(key)
    assert active is not None
    assert active.provider == "codex"
    assert active.session_id == ""
    assert active.working_dir == str(orch.paths.workspace)
    assert active.source_kind == "ductor"


async def test_start_fresh_codex_session_shows_confirmation_when_codex_bucket_exists(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )

    key = SessionKey(chat_id=1)
    await orch._sessions.set_provider_session_state(
        key,
        provider="codex",
        model="gpt-5.2-codex",
        session_id="existing-codex",
        working_dir=str(orch.paths.workspace),
        source_kind="ductor",
    )

    resp = await handle_session_callback(orch, key, "nsc:cxf:0:0")

    assert "Start Fresh Codex Session?" in resp.text


async def test_start_fresh_codex_session_is_blocked_in_docker_mode(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )
    orch._config.docker.enabled = True

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), "nsc:cxf:0:0")

    assert "host mode" in resp.text


async def test_codex_detail_page_shows_richer_session_metadata(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), "nsc:cxd:0:0:0:0")

    assert "Model:" in resp.text
    assert "gpt-5.4" in resp.text
    assert "First prompt:" in resp.text
    assert "first imported prompt" in resp.text
    assert "Latest prompt:" in resp.text
    assert "latest imported prompt" in resp.text
    assert "Last output:" in resp.text
    assert "assistant summary" in resp.text


async def test_codex_detail_page_shows_launch_dir_when_different(
    orch: Orchestrator,
    monkeypatch,
    tmp_path: Path,
) -> None:
    launch_dir = tmp_path / ".ductor" / "workspace"
    launch_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace), launch_dir=str(launch_dir)),
    )

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), "nsc:cxd:0:0:0:0")

    assert "Project:" in resp.text
    assert str(orch.paths.workspace) in resp.text
    assert "Launched from:" in resp.text
    assert str(launch_dir) in resp.text


async def test_codex_detail_page_uses_attach_as_named_label(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), "nsc:cxd:0:0:0:0")

    assert resp.buttons is not None
    labels = [button.text for row in resp.buttons.rows for button in row]
    assert "Attach Named" in labels
    assert "Desktop Resume Command" in labels


async def test_main_desktop_resume_page_shows_exact_command(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )
    key = SessionKey(chat_id=1)
    await orch.attach_codex_import(
        key,
        session_id="sess-import-1",
        working_dir=str(orch.paths.workspace),
    )

    resp = await handle_session_callback(orch, key, "nsc:dxm")

    assert "Desktop Resume Command" in resp.text
    assert "codex resume --include-non-interactive --all --cd" in resp.text
    assert "sess-import-1" in resp.text


async def test_main_desktop_resume_page_falls_back_to_workspace_for_ductor_session(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )
    key = SessionKey(chat_id=1)
    await orch._sessions.set_provider_session_state(
        key,
        provider="codex",
        model="gpt-5.2-codex",
        session_id="sess-ductor-1",
        working_dir="",
        source_kind="ductor",
    )

    resp = await handle_session_callback(orch, key, "nsc:dxm")

    assert str(orch.paths.workspace) in resp.text
    assert "sess-ductor-1" in resp.text


async def test_named_desktop_resume_page_shows_exact_command(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )
    session = orch.import_codex_named_session(
        1,
        session_id="sess-import-1",
        working_dir=str(orch.paths.workspace),
        thread_name="planner",
        prompt_preview="previous prompt",
    )

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), f"nsc:dxn:{session.name}")

    assert f"Target: @{session.name}" in resp.text
    assert "codex resume --include-non-interactive --all --cd" in resp.text
    assert "sess-import-1" in resp.text


async def test_named_desktop_resume_page_falls_back_to_workspace_for_ductor_session(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )
    session = orch.named_sessions.create(
        1,
        provider="codex",
        model="gpt-5.2-codex",
        prompt_preview="previous prompt",
    )
    orch.named_sessions.update_after_response(1, session.name, "sess-ductor-1")

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), f"nsc:dxn:{session.name}")

    assert str(orch.paths.workspace) in resp.text
    assert "sess-ductor-1" in resp.text


async def test_browser_desktop_resume_page_shows_exact_command(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), "nsc:cxdc:0:0:0:0")

    assert "Target: Imported thread" in resp.text
    assert "codex resume --include-non-interactive --all --cd" in resp.text
    assert "sess-import-1" in resp.text
