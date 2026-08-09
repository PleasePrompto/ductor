"""Interactive session selector for named sessions and imported Codex history."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ductor_bot.cli.codex_handoff import build_codex_resume_command
from ductor_bot.cli.codex_history import (
    CodexHistoryBrowser,
    CodexHistoryProject,
    CodexHistorySession,
    load_codex_history_browser,
    search_codex_history,
)
from ductor_bot.i18n import t
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse
from ductor_bot.orchestrator.selectors.utils import format_age
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey
    from ductor_bot.session.manager import ProviderSessionData, SessionData
    from ductor_bot.session.named import NamedSession

logger = logging.getLogger(__name__)

NSC_PREFIX = "nsc:"
_PROJECTS_PER_PAGE = 8
_SESSIONS_PER_PAGE = 6
_PROJECT_PATH_LIMIT = 72
_SESSION_TITLE_LIMIT = 116
_SESSION_SNIPPET_LIMIT = 42
_SEARCH_RESULTS_PER_PAGE = 6


def is_session_selector_callback(data: str) -> bool:
    """Return True if *data* belongs to the session selector."""
    return data.startswith(NSC_PREFIX)


async def session_selector_start(
    orch: Orchestrator,
    key: SessionKey,
) -> SelectorResponse:
    """Build the initial ``/sessions`` response with inline controls."""
    return await _build_root_page(orch, key)


async def handle_session_callback(  # noqa: C901, PLR0911, PLR0912, PLR0915
    orch: Orchestrator,
    key: SessionKey,
    data: str,
) -> SelectorResponse:
    """Route a ``nsc:*`` callback to the correct session selector action."""
    logger.debug("Session selector step=%s", data[:48])
    action = data[len(NSC_PREFIX) :]

    if action == "r":
        return await _build_root_page(orch, key)

    if action == "cxq":
        orch.begin_codex_search(key)
        return await _build_codex_projects_page(page=0, note=t("sessions.codex_search_prompt"))

    if action == "cxqa":
        state = orch.codex_search_state(key)
        if state is None:
            return await _build_codex_projects_page(page=0, note=t("sessions.codex_search_missing"))
        orch.begin_codex_search(
            key,
            working_dir=state.working_dir,
            back_callback=state.back_callback,
        )
        prompt = (
            t("sessions.codex_search_project_prompt")
            if state.working_dir
            else t("sessions.codex_search_prompt")
        )
        return await _response_for_callback(orch, key, state.back_callback, note=prompt)

    if action.startswith("cxqp:"):
        parsed = _parse_ints(action[5:], expected=2)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        project_index, page = parsed
        browser = await _load_codex_browser()
        project = _selected_project(browser, project_index)
        if project is None:
            return await _build_codex_projects_page(page=0, note=t("sessions.unknown_action"))
        orch.begin_codex_search(
            key,
            working_dir=project.working_dir,
            back_callback=f"nsc:cxs:{project_index}:{page}",
        )
        return await _build_codex_sessions_page(
            project_index=project_index, page=page, note=t("sessions.codex_search_project_prompt")
        )

    if action.startswith("cxsr:"):
        return await codex_search_page(orch, key, page=_parse_int(action[5:], default=0))

    if action == "cxsc":
        previous = orch.clear_codex_search(key)
        if previous is not None:
            return await _response_for_callback(orch, key, previous.back_callback)
        return await _build_codex_projects_page(page=0)

    if action == "swm":
        orch.switch_named_target(key, None)
        return await _build_root_page(orch, key, note="Now using the main chat.")

    if action.startswith("sw:"):
        name = action[3:]
        if orch.switch_named_target(key, name):
            session = orch.get_named_session(key.chat_id, name)
            title = _named_title(session) if session is not None else name
            return await _build_root_page(orch, key, note=f"Now using: {title}")
        return await _build_root_page(orch, key, note="That session is no longer available.")

    if action.startswith("ren:"):
        name = action[4:]
        if orch._named_sessions.begin_rename(key, name):
            session = orch.get_named_session(key.chat_id, name)
            title = _named_title(session) if session is not None else name
            return await _build_root_page(
                orch, key, note=f"Send the new title for {title} as your next message."
            )
        return await _build_root_page(orch, key, note="That session is no longer available.")

    if action == "endall":
        count = orch._named_sessions.end_all(key.chat_id)
        note = t("sessions.ended_all_one", count=count) if count else t("sessions.ended_all_none")
        return await _build_root_page(orch, key, note=note)

    if action.startswith("end:"):
        name = action[4:]
        ended = await orch.end_named_session(key.chat_id, name)
        note = (
            t("sessions.ended_one", name=name)
            if ended
            else t("sessions.ended_not_found", name=name)
        )
        return await _build_root_page(orch, key, note=note)

    if action == "dxm":
        return await _build_main_desktop_resume_page(orch, key)

    if action.startswith("dxn:"):
        name = action[4:]
        return await _build_named_desktop_resume_page(orch, key, name)

    if action.startswith("cxp:"):
        page = _parse_int(action[4:], default=0)
        return await _build_codex_projects_page(page=page)

    if action.startswith("cxs:"):
        parsed = _parse_ints(action[4:], expected=2)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        project_index, page = parsed
        return await _build_codex_sessions_page(project_index=project_index, page=page)

    if action.startswith("cxf:"):
        parsed = _parse_ints(action[4:], expected=2)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        project_index, page = parsed
        return await _start_fresh_codex_session(
            orch,
            key,
            project_index=project_index,
            page=page,
            require_confirm=False,
        )

    if action.startswith("cxfy:"):
        parsed = _parse_ints(action[5:], expected=2)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        project_index, page = parsed
        return await _start_fresh_codex_session(
            orch,
            key,
            project_index=project_index,
            page=page,
            require_confirm=True,
        )

    if action.startswith("cxd:"):
        parsed = _parse_ints(action[4:], expected=4)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        project_index, session_index, project_page, session_page = parsed
        return await _build_codex_detail_page(
            project_index=project_index,
            session_index=session_index,
            project_page=project_page,
            session_page=session_page,
        )

    if action.startswith("cxsd:"):
        parsed = _parse_ints(action[5:], expected=5)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        project_index, session_index, project_page, session_page, search_page = parsed
        return await _build_codex_detail_page(
            project_index=project_index,
            session_index=session_index,
            project_page=project_page,
            session_page=session_page,
            back_callback=f"nsc:cxsr:{search_page}",
        )

    if action.startswith("cxdc:"):
        parsed = _parse_ints(action[5:], expected=4)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        project_index, session_index, project_page, session_page = parsed
        return await _build_browser_desktop_resume_page(
            project_index=project_index,
            session_index=session_index,
            project_page=project_page,
            session_page=session_page,
        )

    if action.startswith("cxcf:"):
        parsed = _parse_ints(action[5:], expected=4)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        project_index, session_index, project_page, session_page = parsed
        return await _build_codex_confirm_page(
            project_index=project_index,
            session_index=session_index,
            project_page=project_page,
            session_page=session_page,
        )

    if action.startswith("cxu:"):
        parsed = _parse_ints(action[4:], expected=4)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        return await _attach_codex_import(
            orch,
            key,
            *parsed,
            require_confirm=False,
        )

    if action.startswith("cxsu:"):
        parsed = _parse_ints(action[5:], expected=5)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        project_index, session_index, project_page, session_page, search_page = parsed
        return await _attach_codex_import(
            orch,
            key,
            project_index,
            session_index,
            project_page,
            session_page,
            require_confirm=False,
            back_callback=f"nsc:cxsr:{search_page}",
        )

    if action.startswith("cxuy:"):
        parsed = _parse_ints(action[5:], expected=4)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        return await _attach_codex_import(
            orch,
            key,
            *parsed,
            require_confirm=True,
        )

    if action.startswith("cxsuy:"):
        parsed = _parse_ints(action[6:], expected=5)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        project_index, session_index, project_page, session_page, search_page = parsed
        return await _attach_codex_import(
            orch,
            key,
            project_index,
            session_index,
            project_page,
            session_page,
            require_confirm=True,
            back_callback=f"nsc:cxsr:{search_page}",
        )

    if action.startswith("cxn:"):
        parsed = _parse_ints(action[4:], expected=4)
        if parsed is None:
            return await _build_root_page(orch, key, note=t("sessions.unknown_action"))
        project_index, session_index, project_page, session_page = parsed
        return await _import_codex_named_session(
            orch,
            key,
            project_index=project_index,
            session_index=session_index,
            project_page=project_page,
            session_page=session_page,
        )

    logger.warning("Unknown session selector callback: %s", data)
    return await _build_root_page(orch, key, note=t("sessions.unknown_action"))


def _format_topic_block(topic_sessions: list[SessionData]) -> str:
    """Build the topic sessions section for the selector."""
    if not topic_sessions:
        return ""
    lines: list[str] = [t("sessions.topics_header")]
    for idx, ts in enumerate(topic_sessions, 1):
        name = ts.topic_name or f"Topic #{ts.topic_id}"
        msgs = f"{ts.message_count} msg" if ts.message_count == 1 else f"{ts.message_count} msgs"
        cost = f"${ts.total_cost_usd:.2f}"
        markers = _planner_markers(
            source_kind=ts.source_kind,
            planner_mode=ts.planner_mode,
            planner_waiting=ts.planner_waiting,
        )
        lines.append(f"  {idx}. {name} · {ts.provider}/{ts.model} · {msgs}, {cost}")
        if markers:
            lines.append(f"     {markers}")
    return "\n".join(lines)


def _format_current_block(session: SessionData | None) -> str:
    """Build the current-chat main session section."""
    if session is None or session.topic_id is not None:
        return ""
    msgs = f"{session.message_count} msg" if session.message_count == 1 else f"{session.message_count} msgs"
    cost = f"${session.total_cost_usd:.2f}"
    lines = [t("sessions.current_header")]
    lines.append(f"  1. {session.provider}/{session.model} · {msgs}, {cost}")
    markers = _planner_markers(
        source_kind=session.source_kind,
        planner_mode=session.planner_mode,
        planner_waiting=session.planner_waiting,
    )
    if markers:
        lines.append(f"     {markers}")
    return "\n".join(lines)


def _planner_markers(*, source_kind: str, planner_mode: bool, planner_waiting: bool) -> str:
    """Format UI markers for attached/planner state."""
    bits: list[str] = []
    if source_kind == "codex_import":
        bits.append("attached")
    if planner_mode:
        bits.append("plan:on")
    if planner_waiting:
        bits.append("awaiting reply")
    return " · ".join(bits)


def _current_codex_bucket(session: SessionData | None) -> ProviderSessionData | None:
    if session is None:
        return None
    return session.provider_sessions.get("codex")


def _named_session_buttons(ns: NamedSession) -> list[Button]:
    title = _named_title(ns)
    buttons = [
        Button(text=f"Switch: {title[:36]}", callback_data=f"nsc:sw:{ns.name}"),
        Button(text="Rename", callback_data=f"nsc:ren:{ns.name}"),
        Button(text=t("sessions.btn_end", name=title[:36]), callback_data=f"nsc:end:{ns.name}"),
    ]
    if ns.provider == "codex" and ns.session_id:
        buttons.append(
            Button(
                text=t("sessions.btn_resume_desktop"),
                callback_data=f"nsc:dxn:{ns.name}",
            )
        )
    return buttons


def _named_title(ns: NamedSession) -> str:
    return ns.display_title or ns.prompt_preview or ns.name


def _desktop_resume_text(*, target: str, command: str) -> str:
    return fmt(
        t("sessions.desktop_resume_header"),
        SEP,
        t("sessions.desktop_resume_target", target=target),
        t("sessions.desktop_resume_body"),
        f"```bash\n{command}\n```",
    )


def _desktop_working_dir(raw: str, *, source_kind: str, fallback: str) -> str:
    if raw:
        return raw
    if source_kind == "ductor":
        return fallback
    return ""


async def _load_codex_browser() -> CodexHistoryBrowser:
    return await asyncio.to_thread(load_codex_history_browser)


def _parse_int(raw: str, *, default: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def _parse_ints(raw: str, *, expected: int) -> tuple[int, ...] | None:
    parts = raw.split(":")
    if len(parts) != expected:
        return None
    values: list[int] = []
    for part in parts:
        if not part.lstrip("-").isdigit():
            return None
        values.append(max(0, int(part)))
    return tuple(values)


def _project_page_slice(browser: CodexHistoryBrowser, page: int) -> tuple[int, tuple[CodexHistoryProject, ...]]:
    start = max(0, page) * _PROJECTS_PER_PAGE
    stop = start + _PROJECTS_PER_PAGE
    return start, browser.projects[start:stop]


def _session_page_slice(
    project: CodexHistoryProject,
    page: int,
) -> tuple[int, tuple[CodexHistorySession, ...]]:
    start = max(0, page) * _SESSIONS_PER_PAGE
    stop = start + _SESSIONS_PER_PAGE
    return start, project.sessions[start:stop]


def _selected_project(
    browser: CodexHistoryBrowser,
    project_index: int,
) -> CodexHistoryProject | None:
    if 0 <= project_index < len(browser.projects):
        return browser.projects[project_index]
    return None


def _selected_session(
    browser: CodexHistoryBrowser,
    project_index: int,
    session_index: int,
) -> tuple[CodexHistoryProject, CodexHistorySession] | None:
    project = _selected_project(browser, project_index)
    if project is None:
        return None
    if 0 <= session_index < len(project.sessions):
        return project, project.sessions[session_index]
    return None


def _browser_footer(browser: CodexHistoryBrowser) -> str:
    if browser.skipped_count <= 0:
        return ""
    return t("sessions.codex_skipped", count=browser.skipped_count)


def _format_updated_age(updated_ts: float) -> str:
    if updated_ts <= 0:
        return "?"
    return format_age(max(0.0, time.time() - updated_ts))


def _clip_text(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: max(1, limit - 3)].rstrip()}..."


def _codex_session_title(session: CodexHistorySession) -> str:
    title = session.thread_name
    if session.is_ductor_task:
        return f"(Task) {title}"
    if session.is_subagent:
        return f"(Agent) {title}"
    return f"(D) {title}" if session.is_ductor_touched else title


def _codex_session_mix(project: CodexHistoryProject) -> str:
    task_count = sum(1 for session in project.sessions if session.is_ductor_task)
    agent_count = sum(1 for session in project.sessions if session.is_subagent)
    human_count = max(0, len(project.sessions) - task_count - agent_count)
    return f"H:{human_count} T:{task_count} A:{agent_count}"


def _codex_session_snippet_line(session: CodexHistorySession) -> str:
    parts: list[str] = []
    if session.first_prompt and not session.is_ductor_task:
        parts.append(f"F:{_clip_text(session.first_prompt, _SESSION_SNIPPET_LIMIT)}")
    if session.preview and session.preview != session.first_prompt:
        parts.append(f"L:{_clip_text(session.preview, _SESSION_SNIPPET_LIMIT)}")
    if session.last_output_summary:
        parts.append(f"O:{_clip_text(session.last_output_summary, _SESSION_SNIPPET_LIMIT)}")
    return " | ".join(parts)


def _codex_session_entry_lines(session: CodexHistorySession, *, label: str) -> tuple[str, ...]:
    session_title = _clip_text(_codex_session_title(session), _SESSION_TITLE_LIMIT)
    meta_bits = [_format_updated_age(session.updated_ts)]
    if session.model:
        meta_bits.insert(0, session.model)
    lines = [f"  {label}. {session_title} | {' | '.join(meta_bits)}"]
    snippet_line = _codex_session_snippet_line(session)
    if snippet_line:
        lines.append(f"     {snippet_line}")
    return tuple(lines)


def _codex_session_entry_button(
    session: CodexHistorySession,
    *,
    label: str,
    callback_data: str,
) -> Button:
    return Button(
        text=f"{label}. {_codex_session_title(session)[:22]}",
        callback_data=callback_data,
    )


def _recent_background_task_entries(
    project: CodexHistoryProject,
    visible_indexes: set[int],
    *,
    limit: int = 3,
) -> tuple[tuple[int, CodexHistorySession], ...]:
    entries: list[tuple[int, CodexHistorySession]] = []
    for session_index, session in enumerate(project.sessions):
        if session_index in visible_indexes or not session.is_ductor_task:
            continue
        entries.append((session_index, session))
        if len(entries) >= limit:
            break
    return tuple(entries)


async def _build_root_page(  # noqa: C901, PLR0912, PLR0915
    orch: Orchestrator,
    key: SessionKey,
    *,
    note: str = "",
) -> SelectorResponse:
    sessions = orch.list_named_sessions(key.chat_id)
    selected_target = orch.active_named_target(key)
    current_session = await orch._sessions.get_active(key)
    topic_sessions = await orch.list_topic_sessions(key.chat_id)
    browser = await _load_codex_browser()
    current_block = _format_current_block(current_session)
    topic_block = _format_topic_block(topic_sessions)

    if not sessions and not topic_sessions and not current_block:
        body_lines = [t("sessions.empty"), t("sessions.start_hint")]
        if browser.history_available:
            body_lines.append(t("sessions.codex_browse_hint"))
        body = "\n\n".join(body_lines)
        if note:
            body = f"{note}\n\n{body}"
        buttons = None
        if browser.history_available:
            buttons = ButtonGrid(
                rows=[
                    [Button(text=t("sessions.btn_browse_codex"), callback_data="nsc:cxp:0")],
                ]
            )
        return SelectorResponse(text=fmt(t("sessions.header"), SEP, body), buttons=buttons)

    lines: list[str] = []
    rows: list[list[Button]] = []
    now = time.time()

    if current_block:
        lines.append(current_block)
        current_codex = _current_codex_bucket(current_session)
        if current_codex is not None and current_codex.session_id:
            rows.append(
                [
                    Button(
                        text=t("sessions.btn_resume_desktop"),
                        callback_data="nsc:dxm",
                    )
                ]
            )

    active_label = _named_title(selected_target) if selected_target else "Main chat"
    lines.append(f"**Current target:** {active_label}")
    if selected_target is not None:
        rows.append([Button(text="Use main chat", callback_data="nsc:swm")])

    if topic_block:
        lines.append(topic_block)

    if sessions:
        lines.append(t("sessions.named_header"))
        for idx, ns in enumerate(sessions, 1):
            age = format_age(max(0.0, now - ns.created_at))
            msgs = f"{ns.message_count} msg" if ns.message_count == 1 else f"{ns.message_count} msgs"
            lines.append(
                f"  {idx}. **{_named_title(ns)}** | {ns.provider}/{ns.model}"
                f" | {ns.status} ({msgs}, {age})"
            )
            markers = _planner_markers(
                source_kind=ns.source_kind,
                planner_mode=ns.planner_mode,
                planner_waiting=ns.planner_waiting,
            )
            if markers:
                lines.append(f"     {markers}")
            lines.append(f"     > _{ns.prompt_preview}_")
            rows.append(
                _named_session_buttons(ns)
            )
    elif topic_block:
        lines.append(f"{t('sessions.named_header')}\n  {t('sessions.named_empty')}")

    nav_row = [Button(text=t("sessions.btn_refresh"), callback_data="nsc:r")]
    if browser.history_available:
        nav_row.append(Button(text=t("sessions.btn_browse_codex"), callback_data="nsc:cxp:0"))
    rows.append(nav_row)
    if len(sessions) > 1:
        rows.append([Button(text=t("sessions.btn_end_all"), callback_data="nsc:endall")])

    total = len(sessions) + len(topic_sessions) + (1 if current_block else 0)
    info_lines: list[str] = [t("sessions.active_count", count=total)]
    if note:
        info_lines.append(note)

    text = fmt(
        t("sessions.header"),
        SEP,
        "\n".join(lines),
        SEP,
        "\n".join(info_lines),
        t("sessions.followup_hint"),
    )
    return SelectorResponse(text=text, buttons=ButtonGrid(rows=rows))


async def _build_codex_projects_page(
    *,
    page: int,
    note: str = "",
) -> SelectorResponse:
    browser = await _load_codex_browser()
    start, projects = _project_page_slice(browser, page)
    if not browser.projects:
        body = t("sessions.codex_none")
        footer = _browser_footer(browser)
        if note:
            body = f"{note}\n\n{body}"
        return SelectorResponse(
            text=fmt(t("sessions.codex_header"), SEP, body, footer),
            buttons=ButtonGrid(
                rows=[
                    [Button(text=t("sessions.btn_back"), callback_data="nsc:r")],
                ]
            ),
        )

    lines = [t("sessions.codex_projects")]
    rows: list[list[Button]] = []
    for offset, project in enumerate(projects):
        project_index = start + offset
        lines.append(
            f"  {project_index + 1}. **{project.label}**"
            f" | {len(project.sessions)} | {_codex_session_mix(project)}"
            f" | {_format_updated_age(project.updated_ts)}"
        )
        lines.append(f"     `{_clip_text(project.working_dir, _PROJECT_PATH_LIMIT)}`")
        rows.append(
            [
                Button(
                    text=f"{project_index + 1}. {project.label[:22]}",
                    callback_data=f"nsc:cxs:{project_index}:0",
                )
            ]
        )

    footer_lines = [t("sessions.codex_page", current=page + 1, total=_total_pages(len(browser.projects), _PROJECTS_PER_PAGE))]
    skipped = _browser_footer(browser)
    if skipped:
        footer_lines.append(skipped)
    if note:
        footer_lines.append(note)

    nav_row = [
        Button(text=t("sessions.btn_back"), callback_data="nsc:r"),
        Button(text=t("sessions.btn_refresh"), callback_data=f"nsc:cxp:{page}"),
        Button(text=t("sessions.btn_search_all"), callback_data="nsc:cxq"),
    ]
    if page > 0:
        nav_row.append(Button(text=t("sessions.btn_prev"), callback_data=f"nsc:cxp:{page - 1}"))
    if (page + 1) * _PROJECTS_PER_PAGE < len(browser.projects):
        nav_row.append(Button(text=t("sessions.btn_next"), callback_data=f"nsc:cxp:{page + 1}"))
    rows.append(nav_row)

    return SelectorResponse(
        text=fmt(t("sessions.codex_header"), SEP, "\n".join(lines), SEP, "\n".join(footer_lines)),
        buttons=ButtonGrid(rows=rows),
    )


async def _build_main_desktop_resume_page(
    orch: Orchestrator,
    key: SessionKey,
) -> SelectorResponse:
    active = await orch._sessions.get_active(key)
    codex = _current_codex_bucket(active)
    if active is None or codex is None or not codex.session_id:
        return await _build_root_page(orch, key, note=t("sessions.desktop_resume_unavailable"))
    command = build_codex_resume_command(
        codex.session_id,
        _desktop_working_dir(
            codex.working_dir,
            source_kind=codex.source_kind,
            fallback=str(orch.paths.workspace),
        ),
    )
    return SelectorResponse(
        text=_desktop_resume_text(target=t("sessions.desktop_resume_main_target"), command=command),
        buttons=ButtonGrid(rows=[[Button(text=t("sessions.btn_back"), callback_data="nsc:r")]]),
    )


async def _build_named_desktop_resume_page(
    orch: Orchestrator,
    key: SessionKey,
    name: str,
) -> SelectorResponse:
    ns = orch.get_named_session(key.chat_id, name)
    if ns is None or ns.provider != "codex" or not ns.session_id:
        return await _build_root_page(orch, key, note=t("sessions.desktop_resume_unavailable"))
    command = build_codex_resume_command(
        ns.session_id,
        _desktop_working_dir(
            ns.working_dir,
            source_kind=ns.source_kind,
            fallback=str(orch.paths.workspace),
        ),
    )
    return SelectorResponse(
        text=_desktop_resume_text(target=f"@{ns.name}", command=command),
        buttons=ButtonGrid(rows=[[Button(text=t("sessions.btn_back"), callback_data="nsc:r")]]),
    )


async def _build_codex_sessions_page(
    *,
    project_index: int,
    page: int,
    note: str = "",
) -> SelectorResponse:
    browser = await _load_codex_browser()
    project = _selected_project(browser, project_index)
    if project is None:
        return await _build_codex_projects_page(page=0, note=t("sessions.unknown_action"))

    start, sessions = _session_page_slice(project, page)
    project_page = project_index // _PROJECTS_PER_PAGE
    lines = [
        f"**{project.label}**",
        f"`{_clip_text(project.working_dir, _PROJECT_PATH_LIMIT)}`",
        f"{_codex_session_mix(project)} | D=Ductor T=Task A=Agent",
    ]
    rows: list[list[Button]] = [
        [
            Button(
                text=t("sessions.btn_start_fresh_here"),
                callback_data=f"nsc:cxf:{project_index}:{page}",
            )
        ]
    ]
    rows.append(
        [
            Button(
                text=t("sessions.btn_search_project"),
                callback_data=f"nsc:cxqp:{project_index}:{page}",
            ),
            Button(text=t("sessions.btn_search_all"), callback_data="nsc:cxq"),
        ]
    )
    visible_session_indexes: set[int] = set()
    for offset, session in enumerate(sessions):
        session_index = start + offset
        visible_session_indexes.add(session_index)
        label = str(session_index + 1)
        lines.extend(_codex_session_entry_lines(session, label=label))
        rows.append(
            [
                _codex_session_entry_button(
                    session,
                    label=label,
                    callback_data=(
                        f"nsc:cxd:{project_index}:{session_index}:{project_page}:{page}"
                    ),
                )
            ]
        )


    if page == 0:
        recent_tasks = _recent_background_task_entries(project, visible_session_indexes)
        if recent_tasks:
            lines.append("")
            lines.append("Recent background tasks:")
            for task_number, (session_index, session) in enumerate(recent_tasks, 1):
                label = f"Task {task_number}"
                lines.extend(_codex_session_entry_lines(session, label=label))
                rows.append(
                    [
                        _codex_session_entry_button(
                            session,
                            label=label,
                            callback_data=(
                                f"nsc:cxd:{project_index}:{session_index}:{project_page}:{page}"
                            ),
                        )
                    ]
                )

    footer_lines = [
        t(
            "sessions.codex_page",
            current=page + 1,
            total=_total_pages(len(project.sessions), _SESSIONS_PER_PAGE),
        )
    ]
    if note:
        footer_lines.append(note)

    nav_row = [
        Button(
            text=t("sessions.btn_back"),
            callback_data=f"nsc:cxp:{project_index // _PROJECTS_PER_PAGE}",
        ),
        Button(
            text=t("sessions.btn_refresh"),
            callback_data=f"nsc:cxs:{project_index}:{page}",
        )
    ]
    if page > 0:
        nav_row.append(
            Button(text=t("sessions.btn_prev"), callback_data=f"nsc:cxs:{project_index}:{page - 1}")
        )
    if (page + 1) * _SESSIONS_PER_PAGE < len(project.sessions):
        nav_row.append(
            Button(text=t("sessions.btn_next"), callback_data=f"nsc:cxs:{project_index}:{page + 1}")
        )
    rows.append(nav_row)

    return SelectorResponse(
        text=fmt(t("sessions.codex_header"), SEP, "\n".join(lines), SEP, "\n".join(footer_lines)),
        buttons=ButtonGrid(rows=rows),
    )


async def codex_search_page(orch: Orchestrator, key: SessionKey, *, page: int) -> SelectorResponse:
    """Render stored, scoped search state without putting the query in callbacks."""
    state = orch.codex_search_state(key)
    if state is None:
        return await _build_codex_projects_page(page=0, note=t("sessions.codex_search_missing"))
    browser = await _load_codex_browser()
    matches = search_codex_history(browser, state.query, working_dir=state.working_dir)
    start = max(0, page) * _SEARCH_RESULTS_PER_PAGE
    visible = matches[start : start + _SEARCH_RESULTS_PER_PAGE]
    scope = t("sessions.codex_search_scope_project") if state.working_dir else t("sessions.codex_search_scope_all")
    safe_query = _clip_text(state.query, 80).replace("`", "'")
    lines = [
        t("sessions.codex_search_query", query=safe_query),
        t("sessions.codex_search_scope", scope=scope),
        t("sessions.codex_search_count", count=len(matches)),
    ]
    rows: list[list[Button]] = []
    for offset, result in enumerate(visible, start=1):
        label = str(start + offset)
        lines.extend(_codex_session_entry_lines(result.session, label=label))
        project_page = result.project_index // _PROJECTS_PER_PAGE
        rows.append(
            [
                Button(
                    text=f"▶ {label}. {_codex_session_title(result.session)[:20]}",
                    callback_data=(
                        f"nsc:cxsu:{result.project_index}:{result.session_index}:{project_page}:0:{page}"
                    ),
                ),
                Button(
                    text=t("sessions.btn_details"),
                    callback_data=(
                        f"nsc:cxsd:{result.project_index}:{result.session_index}:{project_page}:0:{page}"
                    ),
                ),
            ]
        )
    if not visible:
        lines.append(t("sessions.codex_search_none"))
    total = _total_pages(len(matches), _SEARCH_RESULTS_PER_PAGE)
    nav_row = [
        Button(text=t("sessions.btn_search_back"), callback_data=state.back_callback),
        Button(text=t("sessions.btn_search_refresh"), callback_data=f"nsc:cxsr:{page}"),
        Button(text=t("sessions.btn_search_again"), callback_data="nsc:cxqa"),
        Button(text=t("sessions.btn_clear"), callback_data="nsc:cxsc"),
    ]
    if page > 0:
        nav_row.append(Button(text=t("sessions.btn_prev"), callback_data=f"nsc:cxsr:{page - 1}"))
    if start + _SEARCH_RESULTS_PER_PAGE < len(matches):
        nav_row.append(Button(text=t("sessions.btn_next"), callback_data=f"nsc:cxsr:{page + 1}"))
    rows.append(nav_row)
    return SelectorResponse(
        text=fmt(
            t("sessions.codex_search_header"), SEP, "\n".join(lines), SEP,
            t("sessions.codex_page", current=page + 1, total=total),
        ),
        buttons=ButtonGrid(rows=rows),
    )


async def _response_for_callback(
    orch: Orchestrator,
    key: SessionKey,
    callback_data: str,
    *,
    note: str = "",
) -> SelectorResponse:
    """Use normal callback routing for the stored scope without exposing query text."""
    if callback_data.startswith("nsc:cxp:"):
        return await _build_codex_projects_page(page=_parse_int(callback_data[7:], default=0), note=note)
    if callback_data.startswith("nsc:cxs:"):
        parsed = _parse_ints(callback_data[8:], expected=2)
        if parsed is not None:
            return await _build_codex_sessions_page(
                project_index=parsed[0], page=parsed[1], note=note
            )
    return await handle_session_callback(orch, key, callback_data)


async def _build_codex_fresh_confirm_page(
    *,
    project_index: int,
    page: int,
) -> SelectorResponse:
    browser = await _load_codex_browser()
    project = _selected_project(browser, project_index)
    if project is None:
        return await _build_codex_projects_page(page=0, note=t("sessions.unknown_action"))
    return SelectorResponse(
        text=fmt(
            t("sessions.codex_fresh_confirm_header"),
            SEP,
            t("sessions.codex_fresh_confirm_body", project=project.label),
        ),
        buttons=ButtonGrid(
            rows=[
                [
                    Button(
                        text=t("sessions.btn_confirm_start_fresh"),
                        callback_data=f"nsc:cxfy:{project_index}:{page}",
                    ),
                    Button(
                        text=t("sessions.btn_cancel"),
                        callback_data=f"nsc:cxs:{project_index}:{page}",
                    ),
                ]
            ]
        ),
    )


async def _build_codex_detail_page(
    *,
    project_index: int,
    session_index: int,
    project_page: int,
    session_page: int,
    note: str = "",
    back_callback: str = "",
) -> SelectorResponse:
    browser = await _load_codex_browser()
    selected = _selected_session(browser, project_index, session_index)
    if selected is None:
        return await _build_codex_projects_page(page=0, note=t("sessions.unknown_action"))
    project, session = selected
    session_title = _codex_session_title(session)

    lines = [
        session_title,
        f"{t('sessions.codex_project_label')} `{project.working_dir}`",
        (
            f"{t('sessions.codex_updated_label')} {_format_updated_age(session.updated_ts)}"
            f" (`{session.updated_at}`)"
        ),
    ]
    if session.model:
        lines.append(f"{t('sessions.codex_model_label')} `{session.model}`")
    if session.first_prompt:
        prompt_label = (
            "Worker instruction:" if session.is_ductor_task else t("sessions.codex_first_prompt_label")
        )
        lines.append(f"{prompt_label} {session.first_prompt}")
    if session.preview and session.preview != session.first_prompt:
        lines.append(f"{t('sessions.codex_latest_prompt_label')} {session.preview}")
    if session.last_output_summary:
        lines.append(f"{t('sessions.codex_output_summary_label')} {session.last_output_summary}")
    if session.source:
        lines.append(f"{t('sessions.codex_source_label')} `{session.source}`")
    if session.cli_version:
        lines.append(f"{t('sessions.codex_cli_label')} `{session.cli_version}`")
    if session.launch_dir:
        lines.append(f"{t('sessions.codex_launch_label')} `{session.launch_dir}`")

    if note:
        lines.append("")
        lines.append(note)

    rows = [
        [
            Button(
                text=t("sessions.btn_use_here"),
                callback_data=f"nsc:cxu:{project_index}:{session_index}:{project_page}:{session_page}",
            )
        ],
        [
            Button(
                text=t("sessions.btn_import_named"),
                callback_data=f"nsc:cxn:{project_index}:{session_index}:{project_page}:{session_page}",
            )
        ],
        [
            Button(
                text=t("sessions.btn_resume_desktop"),
                callback_data=f"nsc:cxdc:{project_index}:{session_index}:{project_page}:{session_page}",
            )
        ],
        [
            Button(
                text=t("sessions.btn_back"),
                callback_data=back_callback or f"nsc:cxs:{project_index}:{session_page}",
            )
        ],
    ]
    return SelectorResponse(
        text=fmt(t("sessions.codex_detail_header"), SEP, "\n".join(lines)),
        buttons=ButtonGrid(rows=rows),
    )


async def _build_browser_desktop_resume_page(
    *,
    project_index: int,
    session_index: int,
    project_page: int,
    session_page: int,
) -> SelectorResponse:
    browser = await _load_codex_browser()
    selected = _selected_session(browser, project_index, session_index)
    if selected is None:
        return await _build_codex_projects_page(page=0, note=t("sessions.unknown_action"))
    _project, session = selected
    command = build_codex_resume_command(session.session_id, session.working_dir)
    return SelectorResponse(
        text=_desktop_resume_text(target=_codex_session_title(session), command=command),
        buttons=ButtonGrid(
            rows=[
                [
                    Button(
                        text=t("sessions.btn_back"),
                        callback_data=f"nsc:cxd:{project_index}:{session_index}:{project_page}:{session_page}",
                    )
                ]
            ]
        ),
    )


async def _build_codex_confirm_page(
    *,
    project_index: int,
    session_index: int,
    project_page: int,
    session_page: int,
    back_callback: str = "",
    confirm_callback: str = "",
) -> SelectorResponse:
    browser = await _load_codex_browser()
    selected = _selected_session(browser, project_index, session_index)
    if selected is None:
        return await _build_codex_projects_page(page=0, note=t("sessions.unknown_action"))
    _project, session = selected
    return SelectorResponse(
        text=fmt(
            t("sessions.codex_confirm_header"),
            SEP,
            t("sessions.codex_confirm_body", thread=_codex_session_title(session)),
        ),
        buttons=ButtonGrid(
            rows=[
                [
                    Button(
                        text=t("sessions.btn_confirm_attach"),
                        callback_data=confirm_callback
                        or f"nsc:cxuy:{project_index}:{session_index}:{project_page}:{session_page}",
                    ),
                    Button(
                        text=t("sessions.btn_cancel"),
                        callback_data=back_callback
                        or f"nsc:cxd:{project_index}:{session_index}:{project_page}:{session_page}",
                    ),
                ]
            ]
        ),
    )


async def _attach_codex_import(  # noqa: PLR0913
    orch: Orchestrator,
    key: SessionKey,
    project_index: int,
    session_index: int,
    project_page: int,
    session_page: int,
    *,
    require_confirm: bool,
    back_callback: str = "",
) -> SelectorResponse:
    browser = await _load_codex_browser()
    selected = _selected_session(browser, project_index, session_index)
    if selected is None:
        return await _build_codex_projects_page(page=0, note=t("sessions.unknown_action"))
    _project, session = selected
    if orch.codex_import_uses_docker():
        return await _build_codex_detail_page(
            project_index=project_index,
            session_index=session_index,
            project_page=project_page,
            session_page=session_page,
            back_callback=back_callback,
            note=t("session.import_docker_unsupported"),
        )
    if not require_confirm and await _has_active_codex_bucket(orch, key):
        return await _build_codex_confirm_page(
            project_index=project_index,
            session_index=session_index,
            project_page=project_page,
            session_page=session_page,
            back_callback=back_callback,
            confirm_callback=(
                f"nsc:cxsuy:{project_index}:{session_index}:{project_page}:{session_page}:"
                f"{back_callback.removeprefix('nsc:cxsr:')}"
                if back_callback.startswith("nsc:cxsr:")
                else ""
            ),
        )
    await orch.attach_codex_import(key, session_id=session.session_id, working_dir=session.working_dir)
    return await _build_root_page(
        orch,
        key,
        note=t("sessions.codex_attach_done", thread=_codex_session_title(session)),
    )


async def _start_fresh_codex_session(
    orch: Orchestrator,
    key: SessionKey,
    *,
    project_index: int,
    page: int,
    require_confirm: bool,
) -> SelectorResponse:
    browser = await _load_codex_browser()
    project = _selected_project(browser, project_index)
    if project is None:
        return await _build_codex_projects_page(page=0, note=t("sessions.unknown_action"))
    if orch.codex_import_uses_docker():
        return await _build_codex_sessions_page(
            project_index=project_index,
            page=page,
            note=t("session.import_docker_unsupported"),
        )
    if not require_confirm and await _has_active_codex_bucket(orch, key):
        return await _build_codex_fresh_confirm_page(project_index=project_index, page=page)
    await orch.start_fresh_codex_session(key, working_dir=project.working_dir)
    return await _build_root_page(
        orch,
        key,
        note=t("sessions.codex_fresh_done", project=project.label),
    )


async def _import_codex_named_session(  # noqa: PLR0913
    orch: Orchestrator,
    key: SessionKey,
    *,
    project_index: int,
    session_index: int,
    project_page: int,
    session_page: int,
) -> SelectorResponse:
    browser = await _load_codex_browser()
    selected = _selected_session(browser, project_index, session_index)
    if selected is None:
        return await _build_codex_projects_page(page=0, note=t("sessions.unknown_action"))
    _project, session = selected
    if orch.codex_import_uses_docker():
        return await _build_codex_detail_page(
            project_index=project_index,
            session_index=session_index,
            project_page=project_page,
            session_page=session_page,
            note=t("session.import_docker_unsupported"),
        )
    imported = orch.import_codex_named_session(
        key.chat_id,
        session_id=session.session_id,
        working_dir=session.working_dir,
        thread_name=session.thread_name,
        prompt_preview=session.summary,
    )
    return await _build_root_page(
        orch,
        key,
        note=t("sessions.codex_import_done", name=imported.name),
    )


async def _has_active_codex_bucket(orch: Orchestrator, key: SessionKey) -> bool:
    active = await orch._sessions.get_active(key)
    if active is None:
        return False
    codex = active.provider_sessions.get("codex")
    if codex is None:
        return False
    return bool(codex.session_id or codex.message_count or codex.source_kind == "codex_import")


def _total_pages(total: int, page_size: int) -> int:
    return max(1, (total + page_size - 1) // page_size)
