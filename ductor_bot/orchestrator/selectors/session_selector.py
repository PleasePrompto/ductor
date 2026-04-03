"""Interactive session selector for viewing and managing named sessions."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ductor_bot.i18n import t
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse
from ductor_bot.orchestrator.selectors.utils import format_age
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.manager import SessionData

logger = logging.getLogger(__name__)

NSC_PREFIX = "nsc:"


def is_session_selector_callback(data: str) -> bool:
    """Return True if *data* belongs to the session selector."""
    return data.startswith(NSC_PREFIX)


async def session_selector_start(
    orch: Orchestrator,
    chat_id: str,
) -> SelectorResponse:
    """Build the initial ``/sessions`` response with inline controls."""
    return await _build_page(orch, chat_id)


async def handle_session_callback(
    orch: Orchestrator,
    chat_id: str,
    data: str,
) -> SelectorResponse:
    """Route a ``nsc:*`` callback to the correct session selector action."""
    logger.debug("Session selector step=%s", data[:40])
    action = data[len(NSC_PREFIX) :]

    if action == "r":
        return await _build_page(orch, chat_id)

    if action == "endall":
        count = orch._named_sessions.end_all(chat_id)
        note = t("sessions.ended_all_one", count=count) if count else t("sessions.ended_all_none")
        return await _build_page(orch, chat_id, note=note)

    if action.startswith("end:"):
        name = action[4:]
        ended = await orch.end_named_session(chat_id, name)
        note = (
            t("sessions.ended_one", name=name)
            if ended
            else t("sessions.ended_not_found", name=name)
        )
        return await _build_page(orch, chat_id, note=note)

    logger.warning("Unknown session selector callback: %s", data)
    return await _build_page(orch, chat_id, note=t("sessions.unknown_action"))


def _format_topic_block(topic_sessions: list[SessionData]) -> str:
    """Build the topic sessions section for the selector."""
    if not topic_sessions:
        return ""
    lines: list[str] = [t("sessions.topics_header")]
    for idx, ts in enumerate(topic_sessions, 1):
        name = ts.topic_name or f"Topic #{ts.topic_id}"
        msgs = f"{ts.message_count} msg" if ts.message_count == 1 else f"{ts.message_count} msgs"
        cost = f"${ts.total_cost_usd:.2f}"
        lines.append(f"  {idx}. {name} · {ts.provider}/{ts.model} · {msgs}, {cost}")
    return "\n".join(lines)


async def _build_page(
    orch: Orchestrator,
    chat_id: str,
    *,
    note: str = "",
) -> SelectorResponse:
    sessions = orch.list_named_sessions(chat_id)
    topic_sessions = await orch.list_topic_sessions(chat_id)
    topic_block = _format_topic_block(topic_sessions)

    if not sessions and not topic_sessions:
        body = t("sessions.empty")
        if note:
            body = f"{note}\n\n{body}"
        return SelectorResponse(
            text=fmt(
                t("sessions.header"),
                SEP,
                body,
                SEP,
                t("sessions.start_hint"),
            ),
        )

    lines: list[str] = []
    rows: list[list[Button]] = []
    now = time.time()

    if topic_block:
        lines.append(topic_block)

    if sessions:
        lines.append(t("sessions.named_header"))
        for idx, ns in enumerate(sessions, 1):
            status_label = ns.status
            age_seconds = now - ns.created_at
            age = format_age(age_seconds)
            provider_label = ns.provider
            msgs = (
                f"{ns.message_count} msg" if ns.message_count == 1 else f"{ns.message_count} msgs"
            )
            lines.append(
                f"  {idx}. **{ns.name}** | {provider_label}/{ns.model}"
                f" | {status_label} ({msgs}, {age})"
            )
            lines.append(f"     > _{ns.prompt_preview}_")
            rows.append(
                [
                    Button(
                        text=t("sessions.btn_end", name=ns.name),
                        callback_data=f"nsc:end:{ns.name}",
                    ),
                ]
            )
    elif topic_block:
        lines.append(f"{t('sessions.named_header')}\n  {t('sessions.named_empty')}")

    nav_row: list[Button] = [
        Button(text=t("sessions.btn_refresh"), callback_data="nsc:r"),
    ]
    rows.append(nav_row)
    if len(sessions) > 1:
        rows.append([Button(text=t("sessions.btn_end_all"), callback_data="nsc:endall")])

    total = len(sessions) + len(topic_sessions)
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
