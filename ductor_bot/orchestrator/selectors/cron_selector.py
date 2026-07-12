"""Interactive cron selector for toggling enabled/disabled jobs."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from ductor_bot.i18n import t
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from ductor_bot.cron.manager import CronJob
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)

CRN_PREFIX = "crn:"
_PAGE_SIZE = 6


def is_cron_selector_callback(data: str) -> bool:
    """Return True if *data* belongs to the cron selector."""
    return data.startswith(CRN_PREFIX)


async def cron_selector_start(
    orch: Orchestrator,
) -> SelectorResponse:
    """Build the initial ``/cron`` response with inline controls."""
    return await _build_page(orch, page=0)


async def handle_cron_callback(
    orch: Orchestrator,
    data: str,
) -> SelectorResponse:
    """Route a ``crn:*`` callback to the correct cron selector action."""
    logger.debug("Cron selector step=%s", data[:40])
    parts = data[len(CRN_PREFIX) :].split(":")
    action = parts[0] if parts else ""
    page = _parse_int(parts[1], default=0) if len(parts) > 1 else 0

    if action in {"r", "n", "p"}:
        page_delta = 0
        if action == "n":
            page_delta = 1
        elif action == "p":
            page_delta = -1
        return await _build_page(orch, page=page + page_delta)

    if action in {"ao", "af"}:
        enabled = action == "ao"
        changed = orch._cron_manager.set_all_enabled(enabled=enabled)
        if changed:
            await _reschedule_now(orch)
        if enabled:
            note = t("cron.all_enabled") if changed else t("cron.already_enabled")
        else:
            note = t("cron.all_disabled") if changed else t("cron.already_disabled")
        return await _build_page(orch, page=page, note=note)

    if action == "t" and len(parts) >= 4:
        slot = _parse_int(parts[2], default=-1)
        fingerprint = parts[3]
        return await _toggle_job(orch, page=page, slot=slot, fingerprint=fingerprint)

    logger.warning("Unknown cron selector callback: %s", data)
    return await _build_page(orch, page=0, note=t("cron.unknown_action"))


async def _toggle_job(
    orch: Orchestrator,
    *,
    page: int,
    slot: int,
    fingerprint: str,
) -> SelectorResponse:
    jobs = orch._cron_manager.list_jobs()
    if not jobs:
        return await _build_page(orch, page=0)

    page_jobs, page, _total_pages = _page_slice(jobs, page)
    if slot < 0 or slot >= len(page_jobs):
        return await _build_page(orch, page=page, note=t("cron.list_changed"))

    job = page_jobs[slot]
    if _fingerprint(job) != fingerprint:
        return await _build_page(orch, page=page, note=t("cron.list_changed"))

    new_enabled = not job.enabled
    changed = orch._cron_manager.set_enabled(job.id, enabled=new_enabled)
    if not changed:
        return await _build_page(orch, page=page, note=t("cron.list_changed"))
    await _reschedule_now(orch)
    state = "enabled" if new_enabled else "disabled"
    note = t("cron.toggled", title=job.title, state=state)
    return await _build_page(orch, page=page, note=note)


def _last_run_label(job: CronJob) -> str:
    """Render the "last: <status>" suffix incl. a delivery-failure hint (#160)."""
    if not job.last_run_status:
        return ""
    label = f" | last: {job.last_run_status}"
    if job.last_delivery_status == "failed":
        label += " (delivery failed)"
    return label


async def _build_page(
    orch: Orchestrator,
    *,
    page: int,
    note: str = "",
) -> SelectorResponse:
    jobs = orch._cron_manager.list_jobs()
    if not jobs:
        return SelectorResponse(
            text=fmt(
                t("cron.header"),
                SEP,
                t("cron.empty"),
                SEP,
                t("cron.empty_hint"),
            ),
        )

    page_jobs, current_page, total_pages = _page_slice(jobs, page)
    start = current_page * _PAGE_SIZE

    lines: list[str] = []
    rows: list[list[Button]] = []
    for idx, job in enumerate(page_jobs):
        number = start + idx + 1
        status_tag = t("cron.status_active") if job.enabled else t("cron.status_paused")
        last_run = _last_run_label(job)
        routing = ""
        if job.chat_id:
            routing = f"\n   → Chat {job.chat_id}"
            if job.topic_id:
                routing += f" / Topic {job.topic_id}"
        lines.append(
            t(
                "cron.job_line",
                number=number,
                title=job.title,
                status=status_tag,
                last_run=last_run,
                schedule=job.schedule,
            )
            + routing
        )
        button_text = (
            t("cron.btn_disable", number=number)
            if job.enabled
            else t("cron.btn_enable", number=number)
        )
        rows.append(
            [
                Button(
                    text=button_text,
                    callback_data=f"crn:t:{current_page}:{idx}:{_fingerprint(job)}",
                ),
            ]
        )

    nav_row: list[Button] = []
    if current_page > 0:
        nav_row.append(
            Button(text=t("cron.btn_prev"), callback_data=f"crn:p:{current_page}"),
        )
    nav_row.append(Button(text=t("cron.btn_refresh"), callback_data=f"crn:r:{current_page}"))
    if current_page < total_pages - 1:
        nav_row.append(Button(text=t("cron.btn_next"), callback_data=f"crn:n:{current_page}"))
    rows.append(nav_row)
    rows.append(
        [
            Button(text=t("cron.btn_all_on"), callback_data=f"crn:ao:{current_page}"),
            Button(text=t("cron.btn_all_off"), callback_data=f"crn:af:{current_page}"),
        ]
    )

    active_count = sum(1 for j in jobs if j.enabled)
    info_parts = [t("cron.info_active", active=active_count, total=len(jobs))]
    if total_pages > 1:
        info_parts.append(t("cron.info_page", current=current_page + 1, total=total_pages))
    info_line = " · ".join(info_parts)
    if note:
        info_line = f"{note}\n{info_line}"

    text = fmt(
        t("cron.header"),
        SEP,
        "\n".join(lines),
        SEP,
        info_line,
    )
    return SelectorResponse(text=text, buttons=ButtonGrid(rows=rows))


async def _reschedule_now(orch: Orchestrator) -> None:
    observer = orch._observers.cron
    if observer is None:
        return
    request_reschedule = getattr(observer, "request_reschedule", None)
    if callable(request_reschedule):
        request_reschedule()
        return
    await observer.reschedule_now()


def _page_slice(jobs: list[CronJob], page: int) -> tuple[list[CronJob], int, int]:
    total_pages = (len(jobs) + _PAGE_SIZE - 1) // _PAGE_SIZE
    current_page = max(0, min(page, total_pages - 1))
    start = current_page * _PAGE_SIZE
    end = start + _PAGE_SIZE
    return jobs[start:end], current_page, total_pages


def _parse_int(raw: str, *, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _fingerprint(job: CronJob) -> str:
    return hashlib.blake2s(job.id.encode("utf-8"), digest_size=4).hexdigest()
