"""Pydantic models for Linear entities."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LinearIssue(BaseModel):
    id: str
    identifier: str
    title: str
    url: str
    state_name: str


class LinearIssueDetails(LinearIssue):
    description: str = Field(default="")


class LinearIssueDraft(BaseModel):
    """AI-generated draft before creating in Linear."""

    title: str
    description: str
    acceptance: str = Field(default="")
    project_key: str = Field(default="")
    priority: int = Field(default=0)


class LinearTeam(BaseModel):
    id: str
    key: str
    name: str
