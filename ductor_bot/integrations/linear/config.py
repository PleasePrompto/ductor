"""Config for Linear integration."""

from __future__ import annotations

from pydantic import BaseModel


class LinearConfig(BaseModel):
    api_token: str = ""
    default_team_id: str = ""
    default_team_key: str = ""
