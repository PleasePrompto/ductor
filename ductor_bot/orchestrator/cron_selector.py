"""Interactive cron selector for toggling enabled/disabled jobs."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.bot.response_format import SEP, fmt

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
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the initial ``/cron`` response with inline controls."""
    return await _build_page(orch, page=0)


async def handle_cron_callback(
    orch: Orchestrator,
    data: str,
) -> tuple[str, InlineKeyboardMarkup | None]:
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
        verb = "enabled" if enabled else "disabled"
        already = "enabled" if enabled else "disabled"
        note = f"All cron jobs {verb}." if changed else f"All cron jobs were already {already}."
        return await _build_page(orch, page=page, note=note)

    if action == "t" and len(parts) >= 4:
        slot = _parse_int(parts[2], default=-1)
        fingerprint = parts[3]
        return await _toggle_job(orch, page=page, slot=slot, fingerprint=fingerprint)

    logger.warning("Unknown cron selector callback: %s", data)
    return await _build_page(orch, page=0, note="Unknown action. Refreshed cron list.")


async def _toggle_job(
    orch: Orchestrator,
    *,
    page: int,
    slot: int,
    fingerprint: str,
) -> tuple[str, InlineKeyboardMarkup | None]:
    jobs = orch._cron_manager.list_jobs()
    if not jobs:
        return await _build_page(orch, page=0)

    page_jobs, page, _total_pages = _page_slice(jobs, page)
    if slot < 0 or slot >= len(page_jobs):
        return await _build_page(orch, page=page, note="Cron list changed. Please try again.")

    job = page_jobs[slot]
    if _fingerprint(job) != fingerprint:
        return await _build_page(orch, page=page, note="Cron list changed. Please try again.")

    new_enabled = not job.enabled
    changed = orch._cron_manager.set_enabled(job.id, enabled=new_enabled)
    if not changed:
        return await _build_page(orch, page=page, note="Cron list changed. Please try again.")
    await _reschedule_now(orch)
    state = "enabled" if new_enabled else "disabled"
    note = f"'{job.title}' {state}."
    return await _build_page(orch, page=page, note=note)


async def _build_page(
    orch: Orchestrator,
    *,
    page: int,
    note: str = "",
) -> tuple[str, InlineKeyboardMarkup | None]:
    jobs = orch._cron_manager.list_jobs()
    if not jobs:
        return (
            fmt(
                "**Scheduled Tasks**",
                SEP,
                "No cron jobs configured.",
                SEP,
                '*Ask your agent: "Run a backup check every day at 9am"*',
            ),
            None,
        )

    page_jobs, current_page, total_pages = _page_slice(jobs, page)
    start = current_page * _PAGE_SIZE

    lines: list[str] = []
    rows: list[list[InlineKeyboardButton]] = []
    for idx, job in enumerate(page_jobs):
        number = start + idx + 1
        state = "ON" if job.enabled else "OFF"
        run_status = f" [{job.last_run_status}]" if job.last_run_status else ""
        lines.append(f"{number}. `{state}` `{job.schedule}` -- {job.title}{run_status}")
        button_text = f"{number}. {'Disable' if job.enabled else 'Enable'}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"crn:t:{current_page}:{idx}:{_fingerprint(job)}",
                ),
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if current_page > 0:
        nav_row.append(
            InlineKeyboardButton(text="<< Prev", callback_data=f"crn:p:{current_page}"),
        )
    nav_row.append(InlineKeyboardButton(text="Refresh", callback_data=f"crn:r:{current_page}"))
    if current_page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="Next >>", callback_data=f"crn:n:{current_page}"))
    rows.append(nav_row)
    rows.append(
        [
            InlineKeyboardButton(text="All ON", callback_data=f"crn:ao:{current_page}"),
            InlineKeyboardButton(text="All OFF", callback_data=f"crn:af:{current_page}"),
        ]
    )

    info_lines = [f"Page {current_page + 1}/{total_pages} · Jobs: {len(jobs)}"]
    if note:
        info_lines.append(note)

    text = fmt(
        "**Scheduled Tasks**",
        SEP,
        "\n".join(lines),
        SEP,
        "\n".join(info_lines),
        "Tap a button to toggle a cron job.",
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _reschedule_now(orch: Orchestrator) -> None:
    observer = orch._cron_observer
    if observer is None:
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
