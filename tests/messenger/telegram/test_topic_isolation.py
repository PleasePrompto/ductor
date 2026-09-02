"""Queueing and interrupting belong to a topic, never to the whole group.

One topic is one session. Two topics in the same supergroup are two unrelated
machines that happen to share a chat id, so a long run in `Salam-Website` must
not queue a message typed in `EMR`, and stopping one must not stop the other.
Both mechanisms were originally keyed by chat id alone, which made every topic
in a group share one queue and one stop button.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

WEBSITE = (-100, 110)
EMR = (-100, 97)


class _FakeProcess:
    """Just enough process to be signalled and inspected."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None


@pytest.fixture
def registry() -> ProcessRegistry:
    return ProcessRegistry()


def _register(reg: ProcessRegistry, chat: int, topic: int, pid: int) -> _FakeProcess:
    proc = _FakeProcess(pid)
    reg.register(chat, proc, label="turn", topic_id=topic)  # type: ignore[arg-type]
    return proc


# ---------------------------------------------------------------------------
# Interrupt
# ---------------------------------------------------------------------------


def test_stopping_one_topic_leaves_the_other_running(
    registry: ProcessRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    signalled: list[int] = []

    def record(pid: int) -> None:
        signalled.append(pid)

    monkeypatch.setattr("ductor_bot.cli.process_registry.interrupt_process", record)
    _register(registry, WEBSITE[0], WEBSITE[1], 1001)
    _register(registry, EMR[0], EMR[1], 2002)

    count = registry.interrupt_all(WEBSITE[0], topic_id=WEBSITE[1])

    assert count == 1
    assert signalled == [1001]


def test_the_interrupt_flag_is_scoped_to_the_topic(
    registry: ProcessRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ignore(_pid: int) -> None:
        return None

    monkeypatch.setattr("ductor_bot.cli.process_registry.interrupt_process", ignore)
    _register(registry, WEBSITE[0], WEBSITE[1], 1001)
    _register(registry, EMR[0], EMR[1], 2002)

    registry.interrupt_all(WEBSITE[0], topic_id=WEBSITE[1])

    assert registry.was_interrupted(WEBSITE[0], WEBSITE[1]) is True
    assert registry.was_interrupted(EMR[0], EMR[1]) is False


def test_clearing_one_topics_interrupt_leaves_the_others(
    registry: ProcessRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ignore(_pid: int) -> None:
        return None

    monkeypatch.setattr("ductor_bot.cli.process_registry.interrupt_process", ignore)
    _register(registry, WEBSITE[0], WEBSITE[1], 1001)
    _register(registry, EMR[0], EMR[1], 2002)
    registry.interrupt_all(WEBSITE[0], topic_id=WEBSITE[1])
    registry.interrupt_all(EMR[0], topic_id=EMR[1])

    registry.clear_interrupt(WEBSITE[0], WEBSITE[1])

    assert registry.was_interrupted(WEBSITE[0], WEBSITE[1]) is False
    assert registry.was_interrupted(EMR[0], EMR[1]) is True


def test_a_chat_wide_interrupt_still_signals_every_topic(
    registry: ProcessRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The group-level escape hatch keeps working for a runaway everything."""
    signalled: list[int] = []

    def record(pid: int) -> None:
        signalled.append(pid)

    monkeypatch.setattr("ductor_bot.cli.process_registry.interrupt_process", record)
    _register(registry, WEBSITE[0], WEBSITE[1], 1001)
    _register(registry, EMR[0], EMR[1], 2002)

    count = registry.interrupt_all(WEBSITE[0])

    assert count == 2
    assert sorted(signalled) == [1001, 2002]
    assert registry.was_interrupted(WEBSITE[0], WEBSITE[1]) is True
    assert registry.was_interrupted(EMR[0], EMR[1]) is True


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


async def _busy_turn(mw: SequentialMiddleware, key: tuple[int, int | None], release):
    """Occupy a topic's lock the way a running turn does."""

    async def handler(_event, _data):
        await release.wait()
        return "done"

    event = SimpleNamespace(
        chat=SimpleNamespace(id=key[0]),
        message_id=1,
        message_thread_id=key[1],
        text="long job",
        is_topic_message=True,
    )
    session_key = SimpleNamespace(lock_key=key)
    return await mw._run_under_lock(handler, event, {}, key[0], session_key)


async def _queue_behind(mw: SequentialMiddleware, key: tuple[int, int | None]):
    event = SimpleNamespace(
        chat=SimpleNamespace(id=key[0]),
        message_id=2,
        message_thread_id=key[1],
        text="second message",
        is_topic_message=True,
    )
    session_key = SimpleNamespace(lock_key=key)

    async def handler(_event, _data):
        return "ran"

    return await mw._run_under_lock(handler, event, {}, key[0], session_key)


def _middleware() -> SequentialMiddleware:
    mw = SequentialMiddleware()
    # Indicators are Telegram calls; the queue's keying is what is under test.
    mw._send_indicator = _noop  # type: ignore[method-assign]
    mw._delete_indicator = _noop  # type: ignore[method-assign]
    mw._edit_indicator = _noop  # type: ignore[method-assign]
    return mw


async def _noop(*_args, **_kwargs) -> None:
    return None


def test_a_message_queued_in_one_topic_is_invisible_to_another() -> None:
    async def scenario() -> None:
        mw = _middleware()
        release = asyncio.Event()
        running = asyncio.create_task(_busy_turn(mw, WEBSITE, release))
        await asyncio.sleep(0)
        waiting = asyncio.create_task(_queue_behind(mw, WEBSITE))
        await asyncio.sleep(0)

        assert mw.has_pending(WEBSITE) is True
        assert mw.has_pending(EMR) is False

        release.set()
        await running
        await waiting

    asyncio.run(scenario())


def test_a_run_in_one_topic_does_not_block_another() -> None:
    """The whole point: two topics are two machines that share a chat id."""

    async def scenario() -> None:
        mw = _middleware()
        release = asyncio.Event()
        running = asyncio.create_task(_busy_turn(mw, WEBSITE, release))
        await asyncio.sleep(0)

        # EMR must run to completion while Salam-Website is still occupied.
        assert await _queue_behind(mw, EMR) == "ran"

        release.set()
        await running

    asyncio.run(scenario())


def test_draining_one_topic_leaves_the_others_queue_intact() -> None:
    async def scenario() -> None:
        mw = _middleware()
        release_a, release_b = asyncio.Event(), asyncio.Event()
        run_a = asyncio.create_task(_busy_turn(mw, WEBSITE, release_a))
        run_b = asyncio.create_task(_busy_turn(mw, EMR, release_b))
        await asyncio.sleep(0)
        wait_a = asyncio.create_task(_queue_behind(mw, WEBSITE))
        wait_b = asyncio.create_task(_queue_behind(mw, EMR))
        await asyncio.sleep(0)

        assert await mw.drain_pending(WEBSITE) == 1
        assert mw.has_pending(EMR) is True

        release_a.set()
        release_b.set()
        await asyncio.gather(run_a, run_b, wait_a, wait_b)

    asyncio.run(scenario())


def test_busy_is_measured_per_topic() -> None:
    async def scenario() -> None:
        mw = _middleware()
        release = asyncio.Event()
        running = asyncio.create_task(_busy_turn(mw, WEBSITE, release))
        await asyncio.sleep(0)

        assert mw.is_busy(WEBSITE) is True
        assert mw.is_busy(EMR) is False
        assert mw.is_chat_busy(WEBSITE[0]) is True

        release.set()
        await running

    asyncio.run(scenario())
