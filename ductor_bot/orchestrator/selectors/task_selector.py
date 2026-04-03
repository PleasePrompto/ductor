"""Interactive task selector for viewing and managing background tasks."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ductor_bot.i18n import t, t_plural
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse
from ductor_bot.orchestrator.selectors.utils import format_age
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from ductor_bot.tasks.hub import TaskHub
    from ductor_bot.tasks.models import TaskEntry

logger = logging.getLogger(__name__)

TSC_PREFIX = "tsc:"

_FINISHED = frozenset({"done", "failed", "cancelled"})


def is_task_selector_callback(data: str) -> bool:
    """Return True if *data* belongs to the task selector."""
    return data.startswith(TSC_PREFIX)


def task_selector_start(
    hub: TaskHub,
    chat_id: str,
) -> SelectorResponse:
    """Build the initial ``/tasks`` response with inline controls."""
    return _build_page(hub, chat_id)


async def handle_task_callback(
    hub: TaskHub,
    chat_id: str,
    data: str,
) -> SelectorResponse:
    """Route a ``tsc:*`` callback to the correct task selector action."""
    logger.debug("Task selector step=%s", data[:40])
    action = data[len(TSC_PREFIX) :]

    if action == "r":
        return _build_page(hub, chat_id)

    if action == "cancelall":
        count = await hub.cancel_all(chat_id)
        note = t_plural("tasks.cancelled", count, count=count) if count else t("tasks.no_running")
        return _build_page(hub, chat_id, note=note)

    if action.startswith("cancel:"):
        task_id = action[7:]
        ok = await hub.cancel(task_id)
        note = (
            t("tasks.task_cancelled", id=task_id) if ok else t("tasks.task_not_running", id=task_id)
        )
        return _build_page(hub, chat_id, note=note)

    if action == "cleanup":
        count = hub.registry.cleanup_finished(chat_id)
        note = (
            t_plural("tasks.cleaned", count, count=count) if count else t("tasks.nothing_to_clean")
        )
        return _build_page(hub, chat_id, note=note)

    logger.warning("Unknown task selector callback: %s", data)
    return _build_page(hub, chat_id, note=t("sessions.unknown_action"))


def _build_page(
    hub: TaskHub,
    chat_id: str,
    *,
    note: str = "",
) -> SelectorResponse:
    all_tasks = hub.registry.list_all(chat_id)
    if not all_tasks:
        body = t("tasks.empty")
        if note:
            body = f"{note}\n\n{body}"
        return SelectorResponse(
            text=fmt(
                t("tasks.header"),
                SEP,
                body,
                SEP,
                t("tasks.hint"),
            ),
        )

    running = [tsk for tsk in all_tasks if tsk.status == "running"]
    waiting = [tsk for tsk in all_tasks if tsk.status == "waiting"]
    finished = [tsk for tsk in all_tasks if tsk.status in _FINISHED]

    lines: list[str] = []
    rows: list[list[Button]] = []
    now = time.time()

    _append_running(running, lines, rows, now)
    _append_waiting(waiting, lines, now, has_prev=bool(running))
    _append_finished(finished, lines, now, has_running=bool(running or waiting))
    _append_nav(rows, finished)

    summary = _summary_line(running, waiting, finished)
    text = fmt(t("tasks.header"), SEP, "\n".join(lines), SEP, summary, note)
    return SelectorResponse(text=text, buttons=ButtonGrid(rows=rows))


def _append_running(
    running: list[TaskEntry],
    lines: list[str],
    rows: list[list[Button]],
    now: float,
) -> None:
    if not running:
        return
    lines.append(t("tasks.running_header"))
    for entry in running:
        lines.append(_format_entry(entry, now))
        rows.append(
            [
                Button(
                    text=t("tasks.btn_cancel", name=entry.name[:20]),
                    callback_data=f"tsc:cancel:{entry.task_id}",
                ),
            ]
        )
    if len(running) > 1:
        rows.append([Button(text=t("tasks.btn_cancel_all"), callback_data="tsc:cancelall")])


def _append_waiting(
    waiting: list[TaskEntry],
    lines: list[str],
    now: float,
    *,
    has_prev: bool,
) -> None:
    if not waiting:
        return
    if has_prev:
        lines.append("")
    lines.append(t("tasks.waiting_header"))
    for entry in waiting:
        lines.append(_format_entry(entry, now))
        if entry.last_question:
            lines.append(f"  ↳ {entry.last_question[:80]}")


def _append_finished(
    finished: list[TaskEntry],
    lines: list[str],
    now: float,
    *,
    has_running: bool,
) -> None:
    if not finished:
        return
    if has_running:
        lines.append("")
    lines.append(t("tasks.finished_header"))
    lines.extend(_format_entry(entry, now) for entry in finished)


def _append_nav(
    rows: list[list[Button]],
    finished: list[TaskEntry],
) -> None:
    nav_row: list[Button] = [
        Button(text=t("tasks.btn_refresh"), callback_data="tsc:r"),
    ]
    if finished:
        nav_row.append(
            Button(text=t("tasks.btn_delete_finished"), callback_data="tsc:cleanup"),
        )
    rows.append(nav_row)


def _summary_line(
    running: list[TaskEntry],
    waiting: list[TaskEntry],
    finished: list[TaskEntry],
) -> str:
    parts = []
    if running:
        parts.append(t("tasks.summary_running", count=len(running)))
    if waiting:
        parts.append(t("tasks.summary_waiting", count=len(waiting)))
    if finished:
        parts.append(t("tasks.summary_finished", count=len(finished)))
    return " · ".join(parts)


def _format_entry(entry: TaskEntry, now: float) -> str:
    """Format a single task entry as a compact line."""
    icon = _status_icon(entry.status)
    if entry.elapsed_seconds:
        duration = f"{entry.elapsed_seconds:.0f}s"
    else:
        duration = format_age(now - entry.created_at)
    provider = f"{entry.provider}/{entry.model}" if entry.provider else ""
    parts = [f"  {icon} **{entry.name}**"]
    if provider:
        parts.append(provider)
    parts.append(f"{entry.status} ({duration})")
    if entry.error:
        parts.append(entry.error[:80])
    return " · ".join(parts)


def _status_icon(status: str) -> str:
    if status == "running":
        return "[...]"
    if status == "done":
        return "[OK]"
    if status == "failed":
        return "[FAIL]"
    if status == "cancelled":
        return "[X]"
    if status == "waiting":
        return "[?]"
    return f"[{status}]"
