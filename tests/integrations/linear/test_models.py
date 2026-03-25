"""Model validation tests for Linear integration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ductor_bot.integrations.linear.models import (
    LinearIssue,
    LinearIssueDetails,
    LinearIssueDraft,
    LinearTeam,
)


def test_linear_issue_validation() -> None:
    issue = LinearIssue(
        id="issue_1",
        identifier="SSU-1",
        title="Test",
        url="https://linear.app/ssu/issue/SSU-1/test",
        state_name="Todo",
    )

    assert issue.identifier == "SSU-1"
    assert issue.state_name == "Todo"


def test_linear_issue_details_defaults() -> None:
    details = LinearIssueDetails(
        id="issue_2",
        identifier="SSU-2",
        title="Detailed",
        url="https://linear.app/ssu/issue/SSU-2/detailed",
        state_name="In Progress",
    )

    assert details.description == ""


def test_linear_issue_draft_defaults() -> None:
    draft = LinearIssueDraft(title="Draft title", description="Draft description")

    assert draft.project_key == ""
    assert draft.priority == 0


def test_linear_team_validation_error() -> None:
    with pytest.raises(ValidationError):
        LinearTeam(id="team", key="SSU")
