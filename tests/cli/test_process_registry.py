"""Tests for process registry."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.cli.process_registry import ProcessRegistry, TrackedProcess


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


def test_multiple_chats_isolated() -> None:
    reg = ProcessRegistry()
    proc1 = _mock_process(pid=1)
    proc2 = _mock_process(pid=2)
    reg.register(chat_id=1, process=proc1, label="main")
    reg.register(chat_id=2, process=proc2, label="main")
