"""Deterministic closure races for marker-only restart admission."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.infra.admission import AdmissionClosed, RestartAdmissionCoordinator


async def test_task_result_runnable_at_zero_observation_finishes_before_restart() -> None:
    coordinator = RestartAdmissionCoordinator()
    started = asyncio.Event()
    release = asyncio.Event()
    delivered: list[str] = []

    async def task_and_result() -> None:
        async with coordinator.lease("task"):
            started.set()
            await release.wait()
            # Completion delivery is nested and retains the task's lease.
            async with coordinator.lease("task-result"):
                delivered.append("result")

    task = asyncio.create_task(task_and_result())
    await started.wait()
    generation = await coordinator.close()
    assert coordinator.active_count(generation) == 1
    assert not await coordinator.wait_for_quiescence(generation, 0.01)
    release.set()
    await task
    assert delivered == ["result"]
    assert await coordinator.wait_for_quiescence(generation, 0.01)


async def test_message_bus_submission_racing_closure_is_not_admitted_twice() -> None:
    coordinator = RestartAdmissionCoordinator()
    await coordinator.close()
    with pytest.raises(AdmissionClosed):
        async with coordinator.lease("message-bus"):
            pass


async def test_telegram_ingress_racing_quiescence_is_rejected_after_atomic_close() -> None:
    coordinator = RestartAdmissionCoordinator()
    await coordinator.close()
    with pytest.raises(AdmissionClosed):
        async with coordinator.lease("telegram-update"):
            pass


async def test_clean_shutdown_never_kills_protected_processes() -> None:
    from ductor_bot.orchestrator.lifecycle import shutdown

    orch = MagicMock()
    orch._process_registry.kill_all_active = AsyncMock(return_value=1)
    orch._api_stop = None
    orch._paths = MagicMock()
    orch._observers.stop_all = AsyncMock()
    orch._docker = None
    await shutdown(orch, emergency=False)
    orch._process_registry.kill_all_active.assert_not_awaited()
