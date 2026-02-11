"""Tests for DependencyQueue: lock management, FIFO ordering, diagnostics."""

from __future__ import annotations

import asyncio
import contextlib

from ductor_bot.cron.dependency_queue import DependencyQueue

# ---------------------------------------------------------------------------
# No dependency runs immediately
# ---------------------------------------------------------------------------


async def test_no_dependency_runs_immediately() -> None:
    """Tasks with dependency=None bypass the queue entirely."""
    dq = DependencyQueue()
    executed = False

    async with dq.acquire("t1", "Task 1", None):
        executed = True

    assert executed is True
    # No locks created for None dependency
    assert dq.get_all_dependencies() == []


async def test_no_dependency_parallel() -> None:
    """Multiple tasks with dependency=None run concurrently."""
    dq = DependencyQueue()
    order: list[str] = []
    barrier = asyncio.Event()

    async def task(name: str) -> None:
        async with dq.acquire(name, name, None):
            order.append(f"{name}_start")
            barrier.set()
            await asyncio.sleep(0)
            order.append(f"{name}_end")

    await asyncio.gather(task("a"), task("b"))
    # Both started (order not guaranteed, but both should complete)
    assert "a_start" in order
    assert "b_start" in order
    assert "a_end" in order
    assert "b_end" in order


# ---------------------------------------------------------------------------
# Same dependency sequential (FIFO)
# ---------------------------------------------------------------------------


async def test_same_dependency_fifo_order() -> None:
    """Three tasks with the same dependency run sequentially in FIFO order."""
    dq = DependencyQueue()
    order: list[str] = []
    gate = asyncio.Event()

    async def task(name: str, wait_for_gate: bool = False) -> None:
        async with dq.acquire(name, name, "shared"):
            if wait_for_gate:
                gate.set()
            order.append(name)
            await asyncio.sleep(0.01)

    # Start task1 first, it acquires the lock
    t1 = asyncio.create_task(task("task1", wait_for_gate=True))
    await gate.wait()

    # Queue task2 and task3 while task1 holds the lock
    t2 = asyncio.create_task(task("task2"))
    t3 = asyncio.create_task(task("task3"))

    await asyncio.gather(t1, t2, t3)
    assert order == ["task1", "task2", "task3"]


async def test_same_dependency_blocks() -> None:
    """A second task on the same dependency waits until the first finishes."""
    dq = DependencyQueue()
    started: list[str] = []
    finished: list[str] = []
    gate = asyncio.Event()

    async def slow_task() -> None:
        async with dq.acquire("slow", "Slow", "dep"):
            started.append("slow")
            gate.set()
            await asyncio.sleep(0.05)
            finished.append("slow")

    async def fast_task() -> None:
        await gate.wait()
        # Small delay to ensure slow_task has the lock
        await asyncio.sleep(0.01)
        async with dq.acquire("fast", "Fast", "dep"):
            started.append("fast")
            # slow must have finished before fast started
            assert "slow" in finished
            finished.append("fast")

    await asyncio.gather(slow_task(), fast_task())
    assert started == ["slow", "fast"]
    assert finished == ["slow", "fast"]


# ---------------------------------------------------------------------------
# Different dependencies run in parallel
# ---------------------------------------------------------------------------


async def test_different_dependencies_parallel() -> None:
    """Tasks with different dependencies run concurrently."""
    dq = DependencyQueue()
    concurrent_count = 0
    max_concurrent = 0
    lock = asyncio.Lock()

    async def task(name: str, dep: str) -> None:
        nonlocal concurrent_count, max_concurrent
        async with dq.acquire(name, name, dep):
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent_count -= 1

    await asyncio.gather(
        task("t1", "dep_a"),
        task("t2", "dep_b"),
        task("t3", "dep_c"),
    )
    assert max_concurrent >= 2  # At least 2 ran concurrently


# ---------------------------------------------------------------------------
# Mixed dependencies
# ---------------------------------------------------------------------------


async def test_mixed_dependencies() -> None:
    """Tasks with same dependency are sequential; different ones are parallel."""
    dq = DependencyQueue()
    order: list[str] = []
    gate_a1 = asyncio.Event()

    async def task(name: str, dep: str | None, signal: asyncio.Event | None = None) -> None:
        async with dq.acquire(name, name, dep):
            order.append(f"{name}_start")
            if signal:
                signal.set()
            await asyncio.sleep(0.02)
            order.append(f"{name}_end")

    # a1 and a2 share "dep_a" -> sequential
    # b1 has "dep_b" -> parallel with dep_a tasks
    # c1 has None -> runs immediately
    t_a1 = asyncio.create_task(task("a1", "dep_a", signal=gate_a1))
    await gate_a1.wait()

    t_a2 = asyncio.create_task(task("a2", "dep_a"))
    t_b1 = asyncio.create_task(task("b1", "dep_b"))
    t_c1 = asyncio.create_task(task("c1", None))

    await asyncio.gather(t_a1, t_a2, t_b1, t_c1)

    # a1 must finish before a2 starts
    assert order.index("a1_end") < order.index("a2_start")
    # b1 and c1 can start before a1 ends (parallel)
    assert "b1_start" in order
    assert "c1_start" in order


# ---------------------------------------------------------------------------
# Cancellation releases lock
# ---------------------------------------------------------------------------


async def test_cancellation_releases_lock() -> None:
    """Cancelling a task releases the dependency lock for the next task."""
    dq = DependencyQueue()
    gate = asyncio.Event()
    result: list[str] = []

    async def cancellable_task() -> None:
        async with dq.acquire("cancel_me", "Cancellable", "dep"):
            gate.set()
            await asyncio.sleep(10)  # Will be cancelled

    async def waiting_task() -> None:
        await gate.wait()
        await asyncio.sleep(0.01)
        async with dq.acquire("waiter", "Waiter", "dep"):
            result.append("waiter_done")

    t1 = asyncio.create_task(cancellable_task())
    t2 = asyncio.create_task(waiting_task())

    await gate.wait()
    await asyncio.sleep(0.01)
    t1.cancel()

    await asyncio.wait_for(t2, timeout=2.0)

    assert result == ["waiter_done"]


# ---------------------------------------------------------------------------
# Timeout releases lock
# ---------------------------------------------------------------------------


async def test_timeout_releases_lock() -> None:
    """A task that times out releases the dependency lock."""
    dq = DependencyQueue()
    result: list[str] = []

    async def slow_task() -> None:
        try:
            async with asyncio.timeout(0.02), dq.acquire("slow", "Slow", "dep"):
                await asyncio.sleep(10)  # Will timeout
        except TimeoutError:
            result.append("timed_out")

    async def follow_up_task() -> None:
        await asyncio.sleep(0.05)  # Wait for slow_task to timeout
        async with dq.acquire("follower", "Follower", "dep"):
            result.append("follower_done")

    await asyncio.gather(slow_task(), follow_up_task())
    assert "timed_out" in result
    assert "follower_done" in result


# ---------------------------------------------------------------------------
# Exception releases lock
# ---------------------------------------------------------------------------


async def test_exception_releases_lock() -> None:
    """An exception inside the context manager releases the lock."""
    dq = DependencyQueue()
    result: list[str] = []

    async def failing_task() -> None:
        try:
            async with dq.acquire("fail", "Failing", "dep"):
                _raise_intentional()
        except RuntimeError:
            result.append("caught")

    async def follow_up_task() -> None:
        await asyncio.sleep(0.02)
        async with dq.acquire("follower", "Follower", "dep"):
            result.append("follower_done")

    await asyncio.gather(failing_task(), follow_up_task())
    assert "caught" in result
    assert "follower_done" in result


def _raise_intentional() -> None:
    """Raise a RuntimeError for testing exception handling."""
    msg = "intentional"
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# get_queue_info diagnostics
# ---------------------------------------------------------------------------


async def test_get_queue_info_empty() -> None:
    """Queue info for unknown dependency shows unlocked, no tasks."""
    dq = DependencyQueue()
    info = dq.get_queue_info("unknown")
    assert info["dependency"] == "unknown"
    assert info["locked"] is False
    assert info["active_task"] is None
    assert info["queue_length"] == 0
    assert info["queued_tasks"] == []


async def test_get_queue_info_active_task() -> None:
    """Queue info shows the currently active task while lock is held."""
    dq = DependencyQueue()
    gate = asyncio.Event()

    async def holder() -> None:
        async with dq.acquire("h1", "Holder", "dep"):
            gate.set()
            await asyncio.sleep(0.1)

    t = asyncio.create_task(holder())
    await gate.wait()

    info = dq.get_queue_info("dep")
    assert info["locked"] is True
    assert info["active_task"] == "Holder"

    t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await t


async def test_get_queue_info_with_queued_tasks() -> None:
    """Queue info shows waiting tasks when lock is held."""
    dq = DependencyQueue()
    gate = asyncio.Event()

    async def holder() -> None:
        async with dq.acquire("h1", "Holder", "dep"):
            gate.set()
            await asyncio.sleep(0.5)

    async def waiter(name: str) -> None:
        await gate.wait()
        await asyncio.sleep(0.01)
        async with dq.acquire(name, name, "dep"):
            pass

    t1 = asyncio.create_task(holder())
    t2 = asyncio.create_task(waiter("W1"))
    t3 = asyncio.create_task(waiter("W2"))

    await gate.wait()
    await asyncio.sleep(0.05)  # Let waiters enqueue

    info = dq.get_queue_info("dep")
    assert info["locked"] is True
    assert info["queue_length"] == 2
    task_labels = [t["task_label"] for t in info["queued_tasks"]]
    assert "W1" in task_labels
    assert "W2" in task_labels

    t1.cancel()
    t2.cancel()
    t3.cancel()
    for t in (t1, t2, t3):
        with contextlib.suppress(asyncio.CancelledError):
            await t


async def test_get_all_dependencies() -> None:
    """get_all_dependencies returns sorted list of known dependency names."""
    dq = DependencyQueue()

    async with dq.acquire("t1", "T1", "beta"):
        pass
    async with dq.acquire("t2", "T2", "alpha"):
        pass

    deps = dq.get_all_dependencies()
    assert deps == ["alpha", "beta"]
