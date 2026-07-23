"""Tests for the MessageBus."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import DeliveryMode, Envelope, LockMode, Origin
from ductor_bot.bus.lock_pool import LockPool

if TYPE_CHECKING:
    import pytest


def _env(**kwargs: object) -> Envelope:
    """Shortcut for creating test envelopes."""
    defaults: dict[str, object] = {"origin": Origin.CRON, "chat_id": 1}
    defaults.update(kwargs)
    return Envelope(**defaults)  # type: ignore[arg-type]


def _mock_transport(name: str = "tg") -> AsyncMock:
    transport = AsyncMock()
    transport.deliver = AsyncMock()
    transport.deliver_broadcast = AsyncMock()
    transport.transport_name = name
    return transport


# -- Basic submit --


async def test_submit_assigns_envelope_id() -> None:
    bus = MessageBus()
    env = _env()
    assert env.envelope_id == ""
    await bus.submit(env)
    assert env.envelope_id != ""


async def test_submit_preserves_existing_id() -> None:
    bus = MessageBus()
    env = _env(envelope_id="custom-id")
    await bus.submit(env)
    assert env.envelope_id == "custom-id"


# -- Delivery modes --


async def test_unicast_calls_deliver() -> None:
    bus = MessageBus()
    t = _mock_transport()
    bus.register_transport(t)

    env = _env(delivery=DeliveryMode.UNICAST)
    await bus.submit(env)

    t.deliver.assert_awaited_once_with(env)
    t.deliver_broadcast.assert_not_awaited()


async def test_broadcast_calls_deliver_broadcast() -> None:
    bus = MessageBus()
    t = _mock_transport()
    bus.register_transport(t)

    env = _env(delivery=DeliveryMode.BROADCAST)
    await bus.submit(env)

    t.deliver_broadcast.assert_awaited_once_with(env)
    t.deliver.assert_not_awaited()


async def test_multiple_transports() -> None:
    bus = MessageBus()
    t1 = _mock_transport()
    t2 = _mock_transport()
    bus.register_transport(t1)
    bus.register_transport(t2)

    env = _env()
    await bus.submit(env)

    t1.deliver.assert_awaited_once()
    t2.deliver.assert_awaited_once()


# -- Locking --


async def test_lock_required_acquires_lock() -> None:
    pool = LockPool()
    bus = MessageBus(lock_pool=pool)

    acquired_inside = False

    async def check_lock(envelope: Envelope) -> None:
        nonlocal acquired_inside
        acquired_inside = pool.is_locked(envelope.lock_key)

    t = _mock_transport()
    t.deliver = check_lock
    bus.register_transport(t)

    env = _env(lock_mode=LockMode.REQUIRED, chat_id=42)
    await bus.submit(env)

    assert acquired_inside is True
    assert pool.is_locked(42) is False  # Released after submit


async def test_lock_none_does_not_lock() -> None:
    pool = LockPool()
    bus = MessageBus(lock_pool=pool)

    locked_inside = False

    async def check_lock(envelope: Envelope) -> None:
        nonlocal locked_inside
        locked_inside = pool.is_locked(envelope.lock_key)

    t = _mock_transport()
    t.deliver = check_lock
    bus.register_transport(t)

    env = _env(lock_mode=LockMode.NONE, chat_id=42)
    await bus.submit(env)

    assert locked_inside is False


# -- Injection --


async def test_injection_updates_result_text() -> None:
    bus = MessageBus()
    t = _mock_transport()
    bus.register_transport(t)

    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(return_value="Injected response")
    bus.set_injector(injector)

    env = _env(
        needs_injection=True,
        prompt="Injected prompt",
        lock_mode=LockMode.REQUIRED,
        chat_id=10,
    )
    await bus.submit(env)

    injector.inject_prompt.assert_awaited_once_with(
        "Injected prompt",
        10,
        f"cron:{env.envelope_id}",
        topic_id=None,
        transport="tg",
    )
    assert env.result_text == "Injected response"
    t.deliver.assert_awaited_once()


async def test_task_result_injection_uses_task_result_label() -> None:
    bus = MessageBus()
    t = _mock_transport()
    bus.register_transport(t)

    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(return_value="Injected response")
    bus.set_injector(injector)

    env = _env(
        origin=Origin.TASK_RESULT,
        envelope_id="abc123",
        needs_injection=True,
        prompt="Task result",
        chat_id=10,
    )
    await bus.submit(env)

    injector.inject_prompt.assert_awaited_once_with(
        "Task result",
        10,
        "task_result:abc123",
        topic_id=None,
        transport="tg",
    )


async def test_injection_skipped_without_prompt() -> None:
    bus = MessageBus()
    t = _mock_transport()
    bus.register_transport(t)

    injector = AsyncMock()
    bus.set_injector(injector)

    env = _env(needs_injection=True, prompt="")
    await bus.submit(env)

    injector.inject_prompt.assert_not_awaited()


async def test_injection_skipped_without_injector() -> None:
    bus = MessageBus()
    t = _mock_transport()
    bus.register_transport(t)

    env = _env(needs_injection=True, prompt="test")
    await bus.submit(env)
    # No error — gracefully skipped
    t.deliver.assert_awaited_once()


async def test_injection_failure_sets_error() -> None:
    bus = MessageBus()
    t = _mock_transport()
    bus.register_transport(t)

    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(side_effect=RuntimeError("CLI crash"))
    bus.set_injector(injector)

    env = _env(
        needs_injection=True,
        prompt="test",
        lock_mode=LockMode.REQUIRED,
    )
    await bus.submit(env)

    assert env.is_error is True
    assert "cron" in env.result_text
    t.deliver.assert_awaited_once()


# -- Transport error resilience --


async def test_transport_error_does_not_crash() -> None:
    bus = MessageBus()
    bad = _mock_transport()
    bad.deliver = AsyncMock(side_effect=RuntimeError("Network error"))
    good = _mock_transport()
    bus.register_transport(bad)
    bus.register_transport(good)

    env = _env()
    await bus.submit(env)

    # Bad transport failed, good transport still called
    good.deliver.assert_awaited_once()


# -- Lock pool property --


def test_lock_pool_property() -> None:
    pool = LockPool()
    bus = MessageBus(lock_pool=pool)
    assert bus.lock_pool is pool


def test_default_lock_pool() -> None:
    bus = MessageBus()
    assert isinstance(bus.lock_pool, LockPool)


# -- No-transport warning --


async def test_no_transports_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    bus = MessageBus()
    env = _env()
    with caplog.at_level("WARNING", logger="ductor_bot.bus.bus"):
        await bus.submit(env)
    assert "No transports registered" in caplog.text
    assert "envelope lost" in caplog.text
