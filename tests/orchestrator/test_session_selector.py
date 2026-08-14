"""Tests for the Telegram session selector including Codex imports."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from ductor_bot.cli.codex_history import (
    CodexHistoryBrowser,
    CodexHistoryProject,
    CodexHistorySession,
)
from ductor_bot.orchestrator.core import CodexSearchState, Orchestrator
from ductor_bot.orchestrator.registry import OrchestratorResult
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


async def test_codex_search_buttons_render_scoped_results_and_keep_callbacks_short(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    browser = _browser(str(orch.paths.workspace))
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: browser,
    )
    key = SessionKey.telegram(1)

    prompt = await handle_session_callback(orch, key, "nsc:cxq")
    assert "Send the terms" in prompt.text
    orch._codex_searches[key.storage_key] = orch._pending_codex_search.pop(
        key.storage_key
    ).__class__(query="Imported", back_callback="nsc:cxp:0")
    result = await handle_session_callback(orch, key, "nsc:cxsr:0")

    assert "Imported thread" in result.text
    assert "🔎 Codex Search" in result.text
    assert "Query: `Imported`" in result.text
    assert "Scope: all projects" in result.text
    assert "Results: 1" in result.text
    callbacks = [button.callback_data for row in result.buttons.rows for button in row]
    assert all(
        "Imported" not in callback and len(callback.encode()) <= 64 for callback in callbacks
    )
    labels = {button.text for row in result.buttons.rows for button in row}
    assert {"📎 Attach & use #1", "ℹ Details #1", "🔎 Search again", "🧹 Clear"} <= labels  # noqa: RUF001
    assert not any(label.startswith("▶") for label in labels)

    detail_callback = next(callback for callback in callbacks if callback.startswith("nsc:cxsd:"))
    detail = await handle_session_callback(orch, key, detail_callback)
    assert "nsc:cxsr:0" in [button.callback_data for row in detail.buttons.rows for button in row]

    orch._codex_searches[key.storage_key] = orch._codex_searches[key.storage_key].__class__(
        query="Imported `escaped`", back_callback="nsc:cxp:0"
    )
    escaped = await handle_session_callback(orch, key, "nsc:cxsr:0")
    assert "`escaped`" not in escaped.text


async def test_search_one_tap_attaches_activates_and_reuses_named_thread(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    browser = _browser(str(orch.paths.workspace))
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: browser,
    )
    key = SessionKey.telegram(1)
    await orch._sessions.set_provider_session_state(
        key,
        provider="codex",
        model="gpt-5.4",
        session_id="existing",
        working_dir=str(orch.paths.workspace),
        source_kind="ductor",
    )
    orch._codex_searches[key.storage_key] = CodexSearchState(
        query="Imported", back_callback="nsc:cxp:0"
    )

    results = await handle_session_callback(orch, key, "nsc:cxsr:0")
    direct = next(
        button.callback_data
        for row in results.buttons.rows
        for button in row
        if button.callback_data.startswith("nsc:cxsn:")
    )
    attached = await handle_session_callback(orch, key, direct)

    assert "✅ Attached and active: Imported thread" in attached.text
    assert "Send your next message normally—it will go to this session." in attached.text
    attached_labels = [button.text for row in attached.buttons.rows for button in row]
    assert {"↩️ Back to results", "📂 Sessions", "↩️ Switch to Main"} <= set(attached_labels)
    assert "nsc:cxsr:0" in [button.callback_data for row in attached.buttons.rows for button in row]
    active = await orch._sessions.get_active(key)
    assert active is not None and active.session_id == "existing"
    selected = orch.active_named_target(key)
    assert selected is not None and selected.session_id == "sess-import-1"
    assert len(orch.list_named_sessions(key.chat_id)) == 1

    again = await handle_session_callback(orch, key, direct)
    assert "✅ Attached and active: Imported thread" in again.text
    assert len(orch.list_named_sessions(key.chat_id)) == 1

    route = AsyncMock(return_value=OrchestratorResult(text="named reply"))
    monkeypatch.setattr("ductor_bot.orchestrator.core.named_session_flow", route)
    monkeypatch.setattr(orch, "_ensure_docker", AsyncMock())
    routed = await orch.handle_message(key, "continue this thread")
    assert routed.text == "named reply"
    route.assert_awaited_once_with(orch, key, selected.name, "continue this thread")

    other_topic = SessionKey.telegram(1, topic_id=9)
    assert orch.active_named_target(other_topic) is None
    await handle_session_callback(orch, other_topic, direct)
    assert orch.active_named_target(other_topic) is not None
    assert orch.active_named_target(key) is not None

    details = await handle_session_callback(
        orch,
        key,
        next(
            button.callback_data
            for row in results.buttons.rows
            for button in row
            if button.callback_data.startswith("nsc:cxsd:")
        ),
    )
    detail_callbacks = [button.callback_data for row in details.buttons.rows for button in row]
    assert "nsc:cxu:0:0:0:0" in detail_callbacks


async def test_project_search_again_preserves_scope_and_menu_labels(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    browser = _browser(str(orch.paths.workspace))
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: browser,
    )
    key = SessionKey.telegram(1)

    project = await handle_session_callback(orch, key, "nsc:cxs:0:0")
    labels = {button.text for row in project.buttons.rows for button in row}
    assert {"🔎 Search this project", "🌐 Search all"} <= labels
    scoped = await handle_session_callback(orch, key, "nsc:cxqp:0:0")
    assert "this project" in scoped.text
    orch._codex_searches[key.storage_key] = orch._pending_codex_search.pop(
        key.storage_key
    ).__class__(
        query="Imported", working_dir=str(orch.paths.workspace), back_callback="nsc:cxs:0:0"
    )
    again = await handle_session_callback(orch, key, "nsc:cxqa")

    assert "this project" in again.text
    pending = orch._pending_codex_search[key.storage_key]
    assert pending.working_dir == str(orch.paths.workspace)


async def test_root_page_shows_readable_active_target_and_switches_to_main(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: CodexHistoryBrowser(projects=()),
    )
    key = SessionKey(chat_id=1, topic_id=44)
    ns = orch._named_sessions.create(
        1, "codex", "gpt-5.4", "Prepare the July release notes", key=key
    )
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

    assert "PC:6 D:0 (2 tasks hidden)" in resp.text
    assert "(PC)=Personal Codex (D)=Ductor" in resp.text
    assert "Recent background tasks:" not in resp.text
    assert "(Task) Review how kit bitmaps work" not in resp.text
    assert "(Task) PM99 metadata335 isolated apply runner smoke" not in resp.text
    assert "first prompt for Review how kit bitmaps work" not in resp.text
    assert resp.buttons is not None
    labels = [button.text for row in resp.buttons.rows for button in row]
    assert not any("Review how kit bitmaps work" in label for label in labels)
    assert not any("PM99 metadata335" in label for label in labels)


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

    assert "(Task) Imported thread" not in resp.text
    assert "(D) Imported thread" not in resp.text
    assert "1 task hidden" in resp.text


async def test_codex_sessions_page_marks_personal_codex_sessions(
    orch: Orchestrator,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.session_selector.load_codex_history_browser",
        lambda: _browser(str(orch.paths.workspace)),
    )

    resp = await handle_session_callback(orch, SessionKey(chat_id=1), "nsc:cxs:0:0")

    assert "(PC) Imported thread" in resp.text
    assert resp.buttons is not None
    labels = [button.text for row in resp.buttons.rows for button in row]
    assert any("(PC) Imported thread" in label for label in labels)


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

    assert "Target: (PC) Imported thread" in resp.text
    assert "codex resume --include-non-interactive --all --cd" in resp.text
    assert "sess-import-1" in resp.text
