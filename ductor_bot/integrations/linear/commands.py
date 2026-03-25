"""Telegram command handlers for Linear integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey


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
    except (RuntimeError, ValueError) as exc:
        return OrchestratorResult(text=f"Failed to load Linear issues: {exc}")

    if not issues:
        return OrchestratorResult(text="No Linear issues found for the configured team.")

    lines = ["Latest Linear issues:"]
    rows: list[list[Button]] = []
    for index, issue in enumerate(issues, start=1):
        lines.append(f"{index}. {_status_emoji(issue.state_name)} {issue.identifier} {issue.title}")
        rows.append([Button(text=issue.identifier, callback_data=f"linear:task:{issue.id}")])

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
    except (RuntimeError, ValueError) as exc:
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
                Button(text="Refine", callback_data=f"linear:refine:{issue.identifier}"),
                Button(text="Comment", callback_data=f"linear:comment:{issue.identifier}"),
                Button(text="State", callback_data=f"linear:status:{issue.identifier}"),
            ]
        ]
    )

    return OrchestratorResult(text=body, buttons=buttons)


async def cmd_create(orch: Orchestrator, key: SessionKey, text: str) -> OrchestratorResult:
    """Handle /create command (placeholder before AI intake implementation)."""
    payload = _extract_command_argument(text, "/create")
    if not payload:
        return OrchestratorResult(text="Напиши описание задачи после /create")

    orch._linear_create_drafts[key.storage_key] = payload
    return OrchestratorResult(text="Задача будет создана через AI intake (Phase 2)")
