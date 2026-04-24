"""Orchestrator: message routing, commands, flows."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator as Orchestrator
    from ductor_bot.orchestrator.registry import OrchestratorResult as OrchestratorResult

__all__ = ["Orchestrator", "OrchestratorResult"]


def __getattr__(name: str) -> object:
    """Load heavy orchestrator exports lazily to avoid import cycles."""
    if name == "Orchestrator":
        from ductor_bot.orchestrator.core import Orchestrator

        return Orchestrator
    if name == "OrchestratorResult":
        from ductor_bot.orchestrator.registry import OrchestratorResult

        return OrchestratorResult
    raise AttributeError(name)
