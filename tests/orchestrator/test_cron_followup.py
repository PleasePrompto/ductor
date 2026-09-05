"""Regression test for attaching an interactive cron answer after expiry/restart."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import time_machine

from ductor_bot.bus.adapters import from_cron_result
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import normal
from ductor_bot.session.key import SessionKey
from ductor_bot.workspace.paths import DuctorPaths


def _response(session_id: str, result: str = "ok") -> AgentResponse:
    return AgentResponse(result=result, session_id=session_id, is_error=False)


@time_machine.travel("2026-08-22 08:00:00+00:00", tick=False)
async def test_cron_answer_keeps_original_question_after_restart_and_idle_expiry(
    tmp_path: Path,
) -> None:
    paths = DuctorPaths(ductor_home=tmp_path)
    config = AgentConfig(idle_timeout_minutes=30)
    first = Orchestrator(config, paths)
    object.__setattr__(
        first._cli_service,
        "execute",
        AsyncMock(return_value=_response("cron-parent")),
    )
    await normal(first, SessionKey.telegram(42), "Initial context")
    await first._cron_followups.record(
        from_cron_result(
            "Scheduled review",
            "Pending items\nItem-42 requires confirmation. Should it be included in the report?",
            "success",
            chat_id=42,
        )
    )

    # A fresh Orchestrator models a process restart. The persisted session is
    # deliberately stale, but the cron context remains durable.
    restarted = Orchestrator(config, paths)
    execute = AsyncMock(return_value=_response("cron-parent", "Handled"))
    object.__setattr__(restarted._cli_service, "execute", execute)
    with time_machine.travel("2026-08-22 09:00:00+00:00", tick=False):
        await normal(restarted, SessionKey.telegram(42), "oui")

    request = execute.call_args.args[0]
    assert request.resume_session == "cron-parent"
    assert "Item-42" in request.prompt
    assert "Should it be included in the report?" in request.prompt
    assert "User response:\noui" in request.prompt
    assert await restarted._cron_followups.consume(SessionKey.telegram(42)) is None
