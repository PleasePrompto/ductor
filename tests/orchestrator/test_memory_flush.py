"""Tests for MemoryFlusher (#77 flush + #80 compaction)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest  # noqa: TC002  -- runtime fixture type (caplog)

from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import MemoryCompactionConfig, MemoryFlushConfig
from ductor_bot.orchestrator.memory_flush import MemoryFlusher
from ductor_bot.session import SessionKey
from ductor_bot.session.manager import ProviderSessionData, SessionData
from ductor_bot.workspace.paths import DuctorPaths


def _session_with_id(session_id: str) -> SessionData:
    s = SessionData(chat_id=101, provider="claude", model="opus")
    s.provider_sessions["claude"] = ProviderSessionData(
        session_id=session_id, message_count=3, total_cost_usd=0.01, total_tokens=100
    )
    return s


def _make_paths(tmp_path: Path, mainmemory_lines: int = 0) -> DuctorPaths:
    paths = DuctorPaths(ductor_home=tmp_path)
    paths.mainmemory_path.parent.mkdir(parents=True, exist_ok=True)
    if mainmemory_lines > 0:
        content = "\n".join(f"- entry {i}" for i in range(mainmemory_lines))
        paths.mainmemory_path.write_text(content, encoding="utf-8")
    else:
        paths.mainmemory_path.write_text("", encoding="utf-8")
    return paths


def _make_flusher(
    tmp_path: Path,
    *,
    mainmemory_lines: int = 0,
    flush_cfg: MemoryFlushConfig | None = None,
    compact_cfg: MemoryCompactionConfig | None = None,
) -> tuple[MemoryFlusher, AsyncMock]:
    cli = AsyncMock()
    cli.execute = AsyncMock(return_value=AgentResponse(result=""))
    paths = _make_paths(tmp_path, mainmemory_lines=mainmemory_lines)
    flusher = MemoryFlusher(
        flush_cfg or MemoryFlushConfig(),
        cli,
        compact_cfg or MemoryCompactionConfig(),
        paths,
    )
    return flusher, cli


# ---------------------------------------------------------------------------
# #77 -- pre-compaction silent flush
# ---------------------------------------------------------------------------


async def test_memory_flusher_fires_silent_turn_after_boundary(tmp_path: Path) -> None:
    """mark_boundary + maybe_flush triggers a silent cli.execute with flush prompt."""
    # Disable compaction so this test isolates flush behavior.
    flusher, cli = _make_flusher(tmp_path, compact_cfg=MemoryCompactionConfig(enabled=False))
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")
    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 1
    request = cli.execute.await_args[0][0]
    assert request.prompt == MemoryFlushConfig().flush_prompt
    assert request.resume_session == "sess-abc"
    assert request.chat_id == 101
    assert request.process_label == "memory_flush"


async def test_memory_flusher_dedup_within_window(tmp_path: Path) -> None:
    """Two boundaries within dedup_seconds cause only one flush."""
    flusher, cli = _make_flusher(
        tmp_path,
        flush_cfg=MemoryFlushConfig(dedup_seconds=300),
        compact_cfg=MemoryCompactionConfig(enabled=False),
    )
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")

    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)
    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 1


async def test_memory_flusher_skips_when_no_session_id(tmp_path: Path) -> None:
    """Flush is a no-op when the session has no resume session_id yet."""
    flusher, cli = _make_flusher(tmp_path, compact_cfg=MemoryCompactionConfig(enabled=False))
    key = SessionKey(chat_id=101)
    session = _session_with_id("")
    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 0


# ---------------------------------------------------------------------------
# #80 -- LLM-driven compaction
# ---------------------------------------------------------------------------


async def test_memory_flusher_runs_compaction_when_file_exceeds_threshold(
    tmp_path: Path,
) -> None:
    """MAINMEMORY.md >= trigger_lines -> flush THEN compaction fire."""
    flusher, cli = _make_flusher(
        tmp_path,
        mainmemory_lines=80,
        compact_cfg=MemoryCompactionConfig(trigger_lines=70, target_lines=40),
    )
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")

    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 2
    flush_call = cli.execute.await_args_list[0][0][0]
    compact_call = cli.execute.await_args_list[1][0][0]
    assert flush_call.process_label == "memory_flush"
    assert compact_call.process_label == "memory_compact"
    assert "MEMORY COMPACTION" in compact_call.prompt
    assert "40" in compact_call.prompt
    assert compact_call.resume_session == "sess-abc"


async def test_memory_flusher_skips_compaction_when_file_under_threshold(
    tmp_path: Path,
) -> None:
    """Small MAINMEMORY.md -> only flush fires, no compaction."""
    flusher, cli = _make_flusher(
        tmp_path,
        mainmemory_lines=10,
        compact_cfg=MemoryCompactionConfig(trigger_lines=70),
    )
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")

    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 1
    assert cli.execute.await_args[0][0].process_label == "memory_flush"


async def test_memory_flusher_skips_compaction_when_disabled(tmp_path: Path) -> None:
    """memory_compaction.enabled=False -> no compaction regardless of size."""
    flusher, cli = _make_flusher(
        tmp_path,
        mainmemory_lines=200,
        compact_cfg=MemoryCompactionConfig(enabled=False, trigger_lines=70),
    )
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")

    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 1
    assert cli.execute.await_args[0][0].process_label == "memory_flush"


async def test_memory_flusher_falls_back_on_bad_prompt_placeholder(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A user-configured compaction prompt with a typo falls back to the default.

    Bug: ``.format()`` raises ``KeyError`` on unknown placeholders, which would
    propagate up through ``maybe_flush`` and suppress the user's real reply.
    """
    bogus_cfg = MemoryCompactionConfig(
        trigger_lines=70,
        target_lines=40,
        prompt="## COMPACT\nrewrite memory {memroy_typo} to {target_lines} lines.",
    )
    flusher, cli = _make_flusher(
        tmp_path,
        mainmemory_lines=80,
        compact_cfg=bogus_cfg,
    )
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")

    flusher.mark_boundary(key)
    with caplog.at_level("WARNING", logger="ductor_bot.orchestrator.memory_flush"):
        await flusher.maybe_flush(key, session)

    # Turn proceeded: both flush + compaction fired.
    assert cli.execute.await_count == 2
    compact_call = cli.execute.await_args_list[1][0][0]
    # Fallback was the default template, so "MEMORY COMPACTION" appears.
    assert "MEMORY COMPACTION" in compact_call.prompt
    assert "40" in compact_call.prompt
    # Warning was logged.
    assert any("invalid placeholder" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# MED #6 -- LockPool integration (serialize against user turns)
# ---------------------------------------------------------------------------


async def test_memory_flusher_serializes_concurrent_flushes_via_lock_pool(
    tmp_path: Path,
) -> None:
    """Two concurrent ``maybe_flush`` calls on the same SessionKey serialize.

    Without the lock pool, both would race and spawn parallel CLI subprocesses
    resuming the same ``session_id``. With the pool attached, only one
    ``cli.execute`` is in-flight at a time.
    """
    active = 0
    peak = 0
    release = asyncio.Event()

    async def blocking_execute(_req: object) -> AgentResponse:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        # Hold the lock until the second task has had a chance to queue.
        try:
            await release.wait()
        finally:
            active -= 1
        return AgentResponse(result="")

    flusher, cli = _make_flusher(
        tmp_path,
        compact_cfg=MemoryCompactionConfig(enabled=False),
        flush_cfg=MemoryFlushConfig(dedup_seconds=0),
    )
    cli.execute.side_effect = blocking_execute
    lock_pool = LockPool()
    flusher.set_lock_pool(lock_pool)

    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")

    # Kick off two concurrent flushes, each marking a boundary first.
    async def one_flush() -> None:
        flusher.mark_boundary(key)
        await flusher.maybe_flush(key, session)

    task_a = asyncio.create_task(one_flush())
    task_b = asyncio.create_task(one_flush())
    # Let both tasks reach the lock acquisition.
    await asyncio.sleep(0.05)
    # Release and wait.
    release.set()
    await asyncio.gather(task_a, task_b)

    # Peak concurrency must never exceed 1 -- the lock serializes them.
    assert peak == 1
    assert cli.execute.await_count >= 1


async def test_memory_flusher_runs_unlocked_without_lock_pool(tmp_path: Path) -> None:
    """Backward-compat: no lock pool -> flusher still works (nullcontext)."""
    flusher, cli = _make_flusher(
        tmp_path,
        compact_cfg=MemoryCompactionConfig(enabled=False),
    )
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")

    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 1


async def test_flush_excludes_appended_system_prompt(tmp_path: Path) -> None:
    """Regression: memory flush must NOT inject append_system_prompt_files."""
    flusher, cli = _make_flusher(tmp_path, compact_cfg=MemoryCompactionConfig(enabled=False))
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")
    flusher.mark_boundary(key)
    await flusher.maybe_flush(key, session)

    assert cli.execute.await_count == 1
    request = cli.execute.await_args[0][0]
    assert request.append_system_prompt is None


async def test_compact_excludes_appended_system_prompt(tmp_path: Path) -> None:
    """Regression: memory compaction must NOT inject append_system_prompt_files."""
    flusher, cli = _make_flusher(tmp_path, mainmemory_lines=100)
    key = SessionKey(chat_id=101)
    session = _session_with_id("sess-abc")
    await flusher.compact(key, session)

    assert cli.execute.await_count == 1
    request = cli.execute.await_args[0][0]
    assert request.append_system_prompt is None
