"""Command handler tests for Linear integration."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

from ductor_bot.integrations.linear.commands import cmd_create, cmd_task, cmd_tasks
from ductor_bot.integrations.linear.config import LinearConfig
from ductor_bot.integrations.linear.models import LinearIssue, LinearIssueDetails
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.session.key import SessionKey


class _DummyConfig:
    def __init__(self) -> None:
        self.linear = LinearConfig(default_team_id="team_1")


class _DummyOrchestrator:
    def __init__(self) -> None:
        self.config = _DummyConfig()
        self.linear_client = type("LinearClientStub", (), {})()
        self._linear_create_drafts: dict[str, str] = {}


def _dummy_orchestrator() -> Orchestrator:
    return cast("Orchestrator", _DummyOrchestrator())


async def test_cmd_tasks_success() -> None:
    orch = _dummy_orchestrator()
    orch.linear_client.list_recent_issues = AsyncMock(
        return_value=[
            LinearIssue(
                id="issue_1",
                identifier="SSU-1",
                title="Task one",
                url="https://linear.app/ssu/issue/SSU-1/task-one",
                state_name="Todo",
            ),
            LinearIssue(
                id="issue_2",
                identifier="SSU-2",
                title="Task two",
                url="https://linear.app/ssu/issue/SSU-2/task-two",
                state_name="In Progress",
            ),
        ]
    )

    result = await cmd_tasks(orch, SessionKey(chat_id=1), "/tasks")

    assert "Latest Linear issues" in result.text
    assert "⬜ SSU-1" in result.text
    assert "🔵 SSU-2" in result.text
    assert result.buttons is not None
    assert result.buttons.rows[0][0].text == "SSU-1"
    assert result.buttons.rows[0][0].callback_data == "linear:task:issue_1"


async def test_cmd_task_success() -> None:
    orch = _dummy_orchestrator()
    orch.linear_client.get_issue = AsyncMock(
        return_value=LinearIssueDetails(
            id="issue_3",
            identifier="SSU-3",
            title="Task details",
            url="https://linear.app/ssu/issue/SSU-3/task-details",
            state_name="Done",
            description="Описание задачи",
        )
    )

    result = await cmd_task(orch, SessionKey(chat_id=1), "/task SSU-3")

    assert "SSU-3: Task details" in result.text
    assert "Status: Done" in result.text
    assert "URL: https://linear.app/ssu/issue/SSU-3/task-details" in result.text
    assert result.buttons is not None
    assert result.buttons.rows[0][0].text == "Refine"


async def test_cmd_task_without_identifier() -> None:
    orch = _dummy_orchestrator()
    result = await cmd_task(orch, SessionKey(chat_id=1), "/task")

    assert result.text == "Usage: /task <identifier>"


async def test_cmd_create_stores_draft() -> None:
    orch = _dummy_orchestrator()
    key = SessionKey(chat_id=42)

    result = await cmd_create(orch, key, "/create Need to build integration")

    assert result.text == "Задача будет создана через AI intake (Phase 2)"
    assert orch._linear_create_drafts[key.storage_key] == "Need to build integration"


async def test_cmd_create_requires_text() -> None:
    orch = _dummy_orchestrator()

    result = await cmd_create(orch, SessionKey(chat_id=42), "/create")

    assert result.text == "Напиши описание задачи после /create"
