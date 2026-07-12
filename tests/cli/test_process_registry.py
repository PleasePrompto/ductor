"""Tests for process registry."""

from __future__ import annotations

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.process_registry import (
    _PRESERVED_LABEL_PREFIXES,
    ProcessRegistry,
    TrackedProcess,
)


def _mock_process(*, pid: int = 1, returncode: int | None = None) -> MagicMock:
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.pid = pid
    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    proc.send_signal = MagicMock()
    return proc


def test_register_returns_tracked() -> None:
    reg = ProcessRegistry()
    proc = _mock_process(pid=42)
    tracked = reg.register(chat_id=1, process=proc, label="main")
    assert isinstance(tracked, TrackedProcess)
    assert tracked.chat_id == 1
    assert tracked.label == "main"


def test_unregister_removes_process() -> None:
    reg = ProcessRegistry()
    proc = _mock_process()
    tracked = reg.register(chat_id=1, process=proc, label="main")
    reg.unregister(tracked)


def test_unregister_idempotent() -> None:
    reg = ProcessRegistry()
    proc = _mock_process()
    tracked = reg.register(chat_id=1, process=proc, label="main")
    reg.unregister(tracked)
    reg.unregister(tracked)  # no error


async def test_kill_all() -> None:
    reg = ProcessRegistry()
    proc = _mock_process(pid=10)
    reg.register(chat_id=1, process=proc, label="main")
    with patch("ductor_bot.cli.process_registry.asyncio.sleep", new_callable=AsyncMock):
        count = await reg.kill_all(chat_id=1)
    assert count == 1


async def test_kill_all_sets_aborted() -> None:
    reg = ProcessRegistry()
    proc = _mock_process()
    reg.register(chat_id=1, process=proc, label="main")
    assert reg.was_aborted(1) is False
    with patch("ductor_bot.cli.process_registry.asyncio.sleep", new_callable=AsyncMock):
        await reg.kill_all(chat_id=1)
    assert reg.was_aborted(1) is True


def test_clear_abort() -> None:
    reg = ProcessRegistry()
    reg._aborted.add(1)
    assert reg.was_aborted(1) is True
    reg.clear_abort(1)
    assert reg.was_aborted(1) is False


async def test_kill_all_empty_returns_zero() -> None:
    reg = ProcessRegistry()
    count = await reg.kill_all(chat_id=999)
    assert count == 0


async def test_kill_all_active_across_chats() -> None:
    reg = ProcessRegistry()
    proc1 = _mock_process(pid=11)
    proc2 = _mock_process(pid=12)
    reg.register(chat_id=1, process=proc1, label="main")
    reg.register(chat_id=2, process=proc2, label="main")

    with patch("ductor_bot.cli.process_registry.asyncio.sleep", new_callable=AsyncMock):
        count = await reg.kill_all_active()

    assert count == 2
    assert reg.has_active(1) is False
    assert reg.has_active(2) is False
    assert reg.was_aborted(1) is True
    assert reg.was_aborted(2) is True


def test_multiple_chats_isolated() -> None:
    reg = ProcessRegistry()
    proc1 = _mock_process(pid=1)
    proc2 = _mock_process(pid=2)
    reg.register(chat_id=1, process=proc1, label="main")
    reg.register(chat_id=2, process=proc2, label="main")
    assert reg.has_active(1) is True
    assert reg.has_active(2) is True
    assert reg.has_active(3) is False


def test_unregister_ignores_foreign_tracked_same_chat() -> None:
    reg = ProcessRegistry()
    proc = _mock_process(pid=11)
    reg.register(chat_id=1, process=proc, label="main")
    foreign = TrackedProcess(process=proc, chat_id=1, label="main")
    reg.unregister(foreign)  # no error
    assert reg.has_active(1) is True


async def test_kill_stale_returns_zero_when_none_stale() -> None:
    reg = ProcessRegistry()
    proc = _mock_process(pid=21)
    reg.register(chat_id=1, process=proc, label="main")
    killed = await reg.kill_stale(max_age_seconds=9999)
    assert killed == 0


async def test_kill_stale_kills_and_unregisters_old_entries() -> None:
    reg = ProcessRegistry()
    old_proc = _mock_process(pid=30)
    fresh_proc = _mock_process(pid=31)
    done_proc = _mock_process(pid=32, returncode=0)

    old = reg.register(chat_id=1, process=old_proc, label="old")
    fresh = reg.register(chat_id=1, process=fresh_proc, label="fresh")
    reg.register(chat_id=1, process=done_proc, label="done")
    old.registered_at = time.time() - 1000
    fresh.registered_at = time.time()

    with patch("ductor_bot.cli.process_registry.asyncio.sleep", new_callable=AsyncMock):
        killed = await reg.kill_stale(max_age_seconds=60)

    assert killed == 1
    assert reg.has_active(1) is True  # fresh process remains


def test_register_stores_topic_id() -> None:
    reg = ProcessRegistry()
    proc = _mock_process(pid=50)
    tracked = reg.register(chat_id=1, process=proc, label="main", topic_id=42)
    assert tracked.topic_id == 42


def test_register_topic_id_defaults_to_none() -> None:
    reg = ProcessRegistry()
    proc = _mock_process(pid=51)
    tracked = reg.register(chat_id=1, process=proc, label="main")
    assert tracked.topic_id is None


def test_has_active_with_topic_id_filters() -> None:
    reg = ProcessRegistry()
    proc1 = _mock_process(pid=60)
    proc2 = _mock_process(pid=61)
    reg.register(chat_id=1, process=proc1, label="main", topic_id=10)
    reg.register(chat_id=1, process=proc2, label="main", topic_id=20)
    assert reg.has_active(1, topic_id=10) is True
    assert reg.has_active(1, topic_id=20) is True
    assert reg.has_active(1, topic_id=99) is False
    assert reg.has_active(1) is True  # no topic_id -> all


def test_has_active_topic_id_ignores_exited() -> None:
    reg = ProcessRegistry()
    done = _mock_process(pid=70, returncode=0)
    alive = _mock_process(pid=71)
    reg.register(chat_id=1, process=done, label="done", topic_id=10)
    reg.register(chat_id=1, process=alive, label="alive", topic_id=20)
    assert reg.has_active(1, topic_id=10) is False
    assert reg.has_active(1, topic_id=20) is True


class TestKillByChatTopicAbortMarker:
    """Topic-scoped /stop marker behavior."""

    async def test_sets_topic_abort_marker(self) -> None:
        reg = ProcessRegistry()
        proc = _mock_process(pid=80)
        reg.register(chat_id=1, process=proc, label="main", topic_id=10)

        with patch(
            "ductor_bot.cli.process_registry._kill_processes",
            new_callable=AsyncMock,
            return_value=1,
        ):
            killed = await reg.kill_by_chat_topic(1, 10)

        assert killed == 1
        assert reg.was_aborted_topic(1, 10) is True
        assert reg.was_aborted(1) is False

    async def test_no_kill_no_marker(self) -> None:
        reg = ProcessRegistry()
        proc = _mock_process(pid=81)
        reg.register(chat_id=1, process=proc, label="main", topic_id=10)

        killed = await reg.kill_by_chat_topic(1, 20)

        assert killed == 0
        assert reg.was_aborted_topic(1, 20) is False
        assert reg.was_aborted_topic(1, 10) is False

    def test_clear_topic_abort_removes_marker(self) -> None:
        reg = ProcessRegistry()
        reg._aborted_topics.add((1, 10))

        assert reg.was_aborted_topic(1, 10) is True
        reg.clear_topic_abort(1, 10)
        assert reg.was_aborted_topic(1, 10) is False

    async def test_topic_a_kill_does_not_kill_topic_b(self) -> None:
        reg = ProcessRegistry()
        proc_a = _mock_process(pid=82)
        proc_b = _mock_process(pid=83)
        reg.register(chat_id=1, process=proc_a, label="main", topic_id=10)
        reg.register(chat_id=1, process=proc_b, label="main", topic_id=20)

        with patch(
            "ductor_bot.cli.process_registry._kill_processes",
            new_callable=AsyncMock,
            return_value=1,
        ):
            killed = await reg.kill_by_chat_topic(1, 10)

        assert killed == 1
        assert reg.has_active(1, topic_id=10) is False
        assert reg.has_active(1, topic_id=20) is True
        assert proc_b.returncode is None

    async def test_kill_by_chat_topic_preserves_protected_labels(self) -> None:
        reg = ProcessRegistry()
        task = reg.register(
            chat_id=1, process=_mock_process(pid=84), label="task:AAAAAAAA", topic_id=10
        )
        task_result = reg.register(
            chat_id=1, process=_mock_process(pid=85), label="task_result:BBBBBBBB", topic_id=10
        )
        named_session = reg.register(
            chat_id=1, process=_mock_process(pid=86), label="ns:review", topic_id=10
        )
        normal = reg.register(chat_id=1, process=_mock_process(pid=87), label="main", topic_id=10)

        with patch(
            "ductor_bot.cli.process_registry._kill_processes",
            new_callable=AsyncMock,
            return_value=1,
        ):
            killed = await reg.kill_by_chat_topic(1, 10)

        assert killed == 1
        remaining_ids = {id(t) for t in reg._processes[1]}
        assert id(normal) not in remaining_ids
        assert {id(task), id(task_result), id(named_session)} <= remaining_ids

        with patch(
            "ductor_bot.cli.process_registry._kill_processes",
            new_callable=AsyncMock,
            return_value=1,
        ):
            task_killed = await reg.kill_for_task("AAAAAAAA")

        assert task_killed == 1
        remaining_ids = {id(t) for t in reg._processes[1]}
        assert id(task) not in remaining_ids
        assert {id(task_result), id(named_session)} <= remaining_ids

    async def test_kill_by_chat_topic_protected_only_returns_zero(self) -> None:
        reg = ProcessRegistry()
        reg.register(chat_id=1, process=_mock_process(pid=88), label="task:AAAAAAAA", topic_id=10)
        reg.register(chat_id=1, process=_mock_process(pid=89), label="ns:review", topic_id=10)

        killed = await reg.kill_by_chat_topic(1, 10)

        assert killed == 0
        assert reg.was_aborted_topic(1, 10) is False
        assert reg.has_active(1, topic_id=10) is True
        assert len(reg._processes[1]) == 2

    async def test_kill_all_sweeps_protected_labels(self) -> None:
        reg = ProcessRegistry()
        reg.register(chat_id=1, process=_mock_process(pid=90), label="task:AAAAAAAA", topic_id=10)
        reg.register(
            chat_id=1, process=_mock_process(pid=91), label="task_result:BBBBBBBB", topic_id=10
        )
        reg.register(chat_id=1, process=_mock_process(pid=92), label="ns:review", topic_id=10)

        with patch(
            "ductor_bot.cli.process_registry._kill_processes",
            new_callable=AsyncMock,
            return_value=3,
        ):
            killed = await reg.kill_all(1)

        assert killed == 3
        assert reg.has_active(1) is False
        assert 1 not in reg._processes

    def test_preserved_prefixes_pinned(self) -> None:
        assert _PRESERVED_LABEL_PREFIXES == ("task:", "task_result:", "ns:")

    async def test_kill_by_chat_topic_preserves_other_topics(self) -> None:
        reg = ProcessRegistry()
        proc_topic_a = _mock_process(pid=93)
        proc_topic_b = _mock_process(pid=94)
        reg.register(chat_id=1, process=proc_topic_a, label="main", topic_id=10)
        reg.register(chat_id=1, process=proc_topic_b, label="main", topic_id=20)

        with patch(
            "ductor_bot.cli.process_registry._kill_processes",
            new_callable=AsyncMock,
            return_value=1,
        ):
            killed = await reg.kill_by_chat_topic(1, 20)

        assert killed == 1
        assert reg.has_active(1, topic_id=10) is True
        assert reg.has_active(1, topic_id=20) is False
        assert proc_topic_a.returncode is None

    async def test_kill_by_chat_topic_none_topic_flat_chat(self) -> None:
        reg = ProcessRegistry()
        flat_normal = reg.register(
            chat_id=1, process=_mock_process(pid=95), label="main", topic_id=None
        )
        flat_task = reg.register(
            chat_id=1, process=_mock_process(pid=96), label="task:AAAAAAAA", topic_id=None
        )
        topic_normal = reg.register(
            chat_id=1, process=_mock_process(pid=97), label="main", topic_id=10
        )

        with patch(
            "ductor_bot.cli.process_registry._kill_processes",
            new_callable=AsyncMock,
            return_value=1,
        ):
            killed = await reg.kill_by_chat_topic(1, None)

        assert killed == 1
        remaining_ids = {id(t) for t in reg._processes[1]}
        assert id(flat_normal) not in remaining_ids
        assert {id(flat_task), id(topic_normal)} <= remaining_ids


async def test_kill_stale_handles_already_exited() -> None:
    reg = ProcessRegistry()
    proc = _mock_process(pid=40, returncode=0)
    tracked = reg.register(chat_id=1, process=proc, label="gone")
    tracked.registered_at = time.time() - 1000

    killed = await reg.kill_stale(max_age_seconds=60)
    assert killed == 0


# -- kill_for_task ---------------------------------------------------------


async def test_kill_for_task_no_matches_returns_zero() -> None:
    """kill_for_task returns 0 and leaves the process registered when label mismatches."""
    reg = ProcessRegistry()
    proc = _mock_process(pid=100)
    reg.register(chat_id=1, process=proc, label="task:AAAAAAAA")

    killed = await reg.kill_for_task("BBBBBBBB")

    assert killed == 0
    # Mismatched entry stays registered (not unregistered).
    assert reg.has_active(1) is True


async def test_kill_for_task_skips_already_exited() -> None:
    """Already-exited processes (returncode set) skip the ladder, mirroring kill_stale."""
    reg = ProcessRegistry()
    proc = _mock_process(pid=101, returncode=0)
    reg.register(chat_id=1, process=proc, label="task:AAAAAAAA")

    killed = await reg.kill_for_task("AAAAAAAA")

    assert killed == 0


async def test_kill_for_task_unregisters_killed_entry() -> None:
    """Each killed entry is unregistered from _processes, mirroring kill_stale."""
    reg = ProcessRegistry()
    proc = _mock_process(pid=102)
    reg.register(chat_id=1, process=proc, label="task:AAAAAAAA")

    with patch(
        "ductor_bot.cli.process_registry._kill_processes",
        new_callable=AsyncMock,
        return_value=1,
    ):
        killed = await reg.kill_for_task("AAAAAAAA")

    assert killed == 1
    # Entry removed from the registry.
    assert reg.has_active(1) is False
    assert 1 not in reg._processes


async def test_kill_for_task_concurrent_register_is_safe() -> None:
    """MED #9: racing register() vs kill_for_task() must not crash.

    With the kill-lock in place, kill_for_task() takes an atomic snapshot
    of its targets; a new register that lands mid-kill either makes it into
    that snapshot or belongs to the next round — it never orphans the
    subprocess and never raises.
    """
    reg = ProcessRegistry()

    # Pre-existing target that kill_for_task will find in its snapshot.
    first = _mock_process(pid=200)
    reg.register(chat_id=1, process=first, label="task:XXXXXXXX")

    # Racing process that tries to register under the same label.
    racing = _mock_process(pid=201)

    async def _racing_register() -> None:
        # Yield a few times so register has a chance to interleave with
        # kill_for_task's await points.
        for _ in range(3):
            await asyncio.sleep(0)
        reg.register(chat_id=1, process=racing, label="task:XXXXXXXX")

    with patch(
        "ductor_bot.cli.process_registry._kill_processes",
        new_callable=AsyncMock,
        return_value=1,
    ):
        killed, _ = await asyncio.gather(
            reg.kill_for_task("XXXXXXXX"),
            _racing_register(),
        )

    # kill_for_task found and killed the pre-existing target cleanly.
    assert killed == 1
    # The racing registration either was swept by the same kill (0 left)
    # or survived for the next round (<=1 left). Both are acceptable — the
    # invariant is: no crash, no exception, registry is consistent.
    remaining = reg._processes.get(1, [])
    assert len(remaining) <= 1


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX sleep binary")
async def test_kill_for_task_kills_real_subprocess() -> None:
    """REAL-subprocess regression test for #92 — mock-only suite cannot catch this bug class.

    The global ``conftest._no_real_process_signals`` fixture patches
    ``terminate_process_tree``/``force_kill_process_tree`` to no-ops so mocked
    PIDs don't reach real processes. For this test we restore the real helpers
    locally so the SIGTERM → SIGKILL ladder actually lands on our child.
    """
    # Restore real signalling just for this test (see conftest._no_real_process_signals).
    from ductor_bot.infra.process_tree import (
        force_kill_process_tree as _real_force_kill,
    )
    from ductor_bot.infra.process_tree import (
        terminate_process_tree as _real_terminate,
    )

    proc = await asyncio.create_subprocess_exec(
        "sleep",
        "30",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        with (
            patch(
                "ductor_bot.cli.process_registry.terminate_process_tree",
                side_effect=_real_terminate,
            ),
            patch(
                "ductor_bot.cli.process_registry.force_kill_process_tree",
                side_effect=_real_force_kill,
            ),
        ):
            reg = ProcessRegistry()
            reg.register(chat_id=1, process=proc, label="task:REAL0001")
            assert proc.returncode is None

            killed = await reg.kill_for_task("REAL0001")
            assert killed == 1

            # SIGTERM grace is 2s + reap ≤ 5s; real kill typically completes in < 2.1s.
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            assert proc.returncode is not None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
