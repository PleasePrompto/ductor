"""Telegram command handlers and callbacks for Linear integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ductor_bot.integrations.linear.models import LinearIssueDraft
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)


def _extract_command_argument(text: str, command: str) -> str:
    stripped = text.strip()
    cmd_with_bot, _, tail_with_bot = stripped.partition(" ")
    cmd_without_bot = cmd_with_bot.split("@", 1)[0]
    if cmd_without_bot != command:
        return tail_with_bot.strip()
    return tail_with_bot.strip()


def _status_emoji(state_name: str) -> str:
    normalized = state_name.casefold()
    if "todo" in normalized or "to do" in normalized:
        return "⬜"
    if "in progress" in normalized or "progress" in normalized:
        return "🔵"
    if "done" in normalized:
        return "✅"
    return "🟡"


async def cmd_tasks(orch: Orchestrator, key: SessionKey, text: str) -> OrchestratorResult:
    """Handle /tasks command and show latest Linear issues."""
    del key, text

    team_id = orch.config.linear.default_team_id.strip()
    if not team_id:
        return OrchestratorResult(
            text="Linear is not configured: set linear.default_team_id in config.json",
        )

    try:
        issues = await orch.linear_client.list_recent_issues(team_id=team_id, limit=15)
    except (RuntimeError, ValueError, TypeError) as exc:
        return OrchestratorResult(text=f"Failed to load Linear issues: {exc}")

    if not issues:
        return OrchestratorResult(text="No Linear issues found for the configured team.")

    lines = ["Latest Linear issues:"]
    rows: list[list[Button]] = []
    for index, issue in enumerate(issues, start=1):
        lines.append(f"{index}. {_status_emoji(issue.state_name)} {issue.identifier} {issue.title}")
        rows.append(
            [Button(text=issue.identifier, callback_data=f"linear:task:{issue.identifier}")]
        )

    return OrchestratorResult(
        text="\n".join(lines),
        buttons=ButtonGrid(rows=rows),
    )


async def cmd_task(orch: Orchestrator, key: SessionKey, text: str) -> OrchestratorResult:
    """Handle /task <identifier> command."""
    del key

    identifier = _extract_command_argument(text, "/task")
    if not identifier:
        return OrchestratorResult(text="Usage: /task <identifier>")

    try:
        issue = await orch.linear_client.get_issue(identifier=identifier)
    except (RuntimeError, ValueError, TypeError) as exc:
        return OrchestratorResult(text=f"Failed to fetch Linear issue: {exc}")

    if issue is None:
        return OrchestratorResult(text=f"Issue {identifier} was not found.")

    description = issue.description.strip() or "Description is empty."
    if len(description) > 2000:
        description = f"{description[:2000].rstrip()}..."

    body = (
        f"{issue.identifier}: {issue.title}\n"
        f"Status: {issue.state_name}\n"
        f"URL: {issue.url}\n\n"
        f"{description}"
    )

    buttons = ButtonGrid(
        rows=[
            [
                Button(text="Проработать", callback_data=f"linear:refine:{issue.identifier}"),
                Button(text="Комментарий", callback_data=f"linear:comment:{issue.identifier}"),
                Button(text="Статус", callback_data=f"linear:status:{issue.identifier}"),
            ]
        ]
    )

    return OrchestratorResult(text=body, buttons=buttons)


async def cmd_create(orch: Orchestrator, key: SessionKey, text: str) -> OrchestratorResult:
    """Handle /create command with AI intake draft generation."""
    payload = _extract_command_argument(text, "/create")
    if not payload:
        return OrchestratorResult(text="Напиши описание задачи после /create")

    cfg = orch.config.intake
    try:
        from ductor_bot.integrations.linear.intake import structure_task

        draft = await structure_task(
            payload,
            provider=cfg.provider,
            model=cfg.model,
            api_key=cfg.api_key,
        )
    except Exception:
        logger.exception("AI intake failed")
        draft = LinearIssueDraft(title=payload[:80], description=payload)

    orch._linear_create_drafts[key.storage_key] = draft

    preview = (
        "📋 Задача (draft):\n\n"
        f"**{draft.title}**\n\n"
        f"{draft.description}\n\n"
        f"Acceptance: {draft.acceptance or '—'}\n"
        f"Priority: {draft.priority}"
    )

    buttons = ButtonGrid(
        rows=[
            [
                Button(text="✅ Создать", callback_data="linear:draft:confirm"),
                Button(text="✏️ Изменить", callback_data="linear:draft:edit"),
                Button(text="❌ Отмена", callback_data="linear:draft:cancel"),
            ]
        ]
    )

    return OrchestratorResult(text=preview, buttons=buttons)


async def handle_linear_callback(
    orch: Orchestrator,
    key: SessionKey,
    callback_data: str,
) -> OrchestratorResult | None:
    """Route linear:* callbacks."""
    parts = callback_data.split(":")
    if len(parts) < 3 or parts[0] != "linear":
        return None

    action = parts[1]
    value = parts[2]

    if action == "draft":
        return await _handle_draft_callback(orch, key, value)

    if action == "task":
        try:
            issue = await orch.linear_client.get_issue(identifier=value)
        except (RuntimeError, ValueError, TypeError) as exc:
            return OrchestratorResult(text=f"Failed to fetch Linear issue: {exc}")
        if not issue:
            return OrchestratorResult(text=f"Issue {value} not found")
        return await cmd_task(orch, key, f"/task {issue.identifier}")

    return None


async def _handle_draft_callback(
    orch: Orchestrator,
    key: SessionKey,
    action: str,
) -> OrchestratorResult:
    draft_obj = orch._linear_create_drafts.pop(key.storage_key, None)
    draft = draft_obj if isinstance(draft_obj, LinearIssueDraft) else None

    if action == "cancel":
        return OrchestratorResult(text="Создание отменено.")

    if action == "edit":
        if draft is not None:
            orch._linear_create_drafts[key.storage_key] = draft
        return OrchestratorResult(text="Отправь исправленное описание - я пересоберу задачу.")

    if action != "confirm":
        return OrchestratorResult(text="Unknown action")

    if draft is None:
        return OrchestratorResult(text="Draft not found. Use /create again.")

    team_id = orch.config.linear.default_team_id
    if not team_id.strip():
        return OrchestratorResult(text="Linear team is not configured.")

    try:
        issue = await orch.linear_client.create_issue(
            team_id=team_id,
            title=draft.title,
            description=f"{draft.description}\n\n## Acceptance\n{draft.acceptance}",
        )
    except Exception as exc:
        text = f"Ошибка создания: {exc}"
    else:
        text = f"✅ Создано: {issue.identifier}\n{issue.url}"
    return OrchestratorResult(text=text)
