"""Tests for Codex thread writer leases."""

from __future__ import annotations

import asyncio

from ductor_bot.cli.codex_thread_lock import codex_thread_lease


async def test_same_thread_leases_are_serialized() -> None:
    entered: list[str] = []
    release = asyncio.Event()

    async def first() -> None:
        async with codex_thread_lease("thread-1", owner="first"):
            entered.append("first")
            await release.wait()

    async def second() -> None:
        async with codex_thread_lease("thread-1", owner="second"):
            entered.append("second")

    first_task = asyncio.create_task(first())
    await asyncio.sleep(0)
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0.01)

    assert entered == ["first"]
    release.set()
    await asyncio.gather(first_task, second_task)
    assert entered == ["first", "second"]


async def test_different_threads_can_run_together() -> None:
    entered: list[str] = []
    release = asyncio.Event()

    async def run(thread_id: str) -> None:
        async with codex_thread_lease(thread_id, owner=thread_id):
            entered.append(thread_id)
            await release.wait()

    tasks = [asyncio.create_task(run("thread-a")), asyncio.create_task(run("thread-b"))]
    await asyncio.sleep(0.01)
    assert set(entered) == {"thread-a", "thread-b"}
    release.set()
    await asyncio.gather(*tasks)
