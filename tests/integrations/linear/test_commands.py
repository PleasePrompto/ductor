"""Command handler tests for Linear integration."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, patch

from ductor_bot.integrations.linear.commands import (
    cmd_create,
    cmd_task,
    cmd_tasks,
    handle_linear_callback,
)
from ductor_bot.integrations.linear.config import IntakeConfig, LinearConfig
from ductor_bot.integrations.linear.models import LinearIssue, LinearIssueDetails, LinearIssueDraft
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.session.key import SessionKey


class _DummyConfig:
    def __init__(self) -> None:
        self.linear = LinearConfig(default_team_id="team_1")
        self.intake = IntakeConfig(provider="passthrough")


class _DummyOrchestrator:
    def __init__(self) -> None:
        self.config = _DummyConfig()
        self.linear_client = type("LinearClientStub", (), {})()
        self._linear_create_drafts: dict[str, LinearIssueDraft] = {}


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
    assert result.buttons.rows[0][0].callback_data == "linear:task:SSU-1"


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
    assert result.buttons.rows[0][0].text == "Проработать"


async def test_cmd_task_without_identifier() -> None:
    orch = _dummy_orchestrator()
    result = await cmd_task(orch, SessionKey(chat_id=1), "/task")

    assert result.text == "Usage: /task <identifier>"


async def test_cmd_create_stores_draft_preview() -> None:
    orch = _dummy_orchestrator()
    key = SessionKey(chat_id=42)

    result = await cmd_create(orch, key, "/create Need to build integration")

    assert "📋 Задача (draft):" in result.text
    assert "**Need to build integration**" in result.text
    assert result.buttons is not None
    assert result.buttons.rows[0][0].callback_data == "linear:draft:confirm"

    stored = orch._linear_create_drafts[key.storage_key]
    assert stored.title == "Need to build integration"
    assert stored.description == "Need to build integration"


async def test_cmd_create_fallback_when_ai_fails() -> None:
    orch = _dummy_orchestrator()
    orch.config.intake = IntakeConfig(provider="openai", api_key="test")
    key = SessionKey(chat_id=7)

    with patch(
        "ductor_bot.integrations.linear.intake.structure_task",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        result = await cmd_create(orch, key, "/create Build integration draft")

    assert "📋 Задача (draft):" in result.text
    assert "Build integration draft" in result.text
    stored = orch._linear_create_drafts[key.storage_key]
    assert stored.title == "Build integration draft"


async def test_cmd_create_requires_text() -> None:
    orch = _dummy_orchestrator()

    result = await cmd_create(orch, SessionKey(chat_id=42), "/create")

    assert result.text == "Напиши описание задачи после /create"


async def test_handle_linear_callback_draft_cancel() -> None:
    orch = _dummy_orchestrator()
    key = SessionKey(chat_id=1)
    orch._linear_create_drafts[key.storage_key] = LinearIssueDraft(title="t", description="d")

    result = await handle_linear_callback(orch, key, "linear:draft:cancel")

    assert result is not None
    assert result.text == "Создание отменено."
    assert key.storage_key not in orch._linear_create_drafts


async def test_handle_linear_callback_draft_edit() -> None:
    orch = _dummy_orchestrator()
    key = SessionKey(chat_id=1)
    draft = LinearIssueDraft(title="t", description="d")
    orch._linear_create_drafts[key.storage_key] = draft

    result = await handle_linear_callback(orch, key, "linear:draft:edit")

    assert result is not None
    assert "Отправь исправленное описание" in result.text
    assert orch._linear_create_drafts[key.storage_key] == draft


async def test_handle_linear_callback_draft_confirm_success() -> None:
    orch = _dummy_orchestrator()
    key = SessionKey(chat_id=1)
    orch._linear_create_drafts[key.storage_key] = LinearIssueDraft(
        title="Title",
        description="Body",
        acceptance="- ok",
        priority=3,
    )
    orch.linear_client.create_issue = AsyncMock(
        return_value=LinearIssue(
            id="issue_9",
            identifier="SSU-9",
            title="Title",
            url="https://linear.app/ssu/issue/SSU-9/title",
            state_name="Todo",
        )
    )

    result = await handle_linear_callback(orch, key, "linear:draft:confirm")

    assert result is not None
    assert "✅ Создано: SSU-9" in result.text
    orch.linear_client.create_issue.assert_awaited_once()
    call_kwargs = orch.linear_client.create_issue.call_args.kwargs
    assert call_kwargs["team_id"] == "team_1"
    assert "## Acceptance" in call_kwargs["description"]


async def test_handle_linear_callback_task_routes_to_cmd_task() -> None:
    orch = _dummy_orchestrator()
    details = LinearIssueDetails(
        id="issue_2",
        identifier="SSU-2",
        title="Task two",
        url="https://linear.app/ssu/issue/SSU-2/task-two",
        state_name="Todo",
        description="Desc",
    )
    orch.linear_client.get_issue = AsyncMock(side_effect=[details, details])

    result = await handle_linear_callback(orch, SessionKey(chat_id=2), "linear:task:SSU-2")

    assert result is not None
    assert "SSU-2: Task two" in result.text


async def test_handle_linear_callback_unknown_namespace_returns_none() -> None:
    orch = _dummy_orchestrator()

    result = await handle_linear_callback(orch, SessionKey(chat_id=2), "abc:def:ghi")

    assert result is None
