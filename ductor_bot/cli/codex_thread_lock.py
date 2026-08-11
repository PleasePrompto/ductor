"""One-writer leases for Codex threads.

Codex permits multiple processes in the same working directory, but its
thread store permits only one active writer for a given thread. Ductor has
several independent CLI services in one supervisor process, and there may
also be more than one Ductor process on a host, so the lease is keyed by
CODEX_HOME and thread ID rather than by working directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]


_local_locks: dict[str, asyncio.Lock] = {}
_local_locks_guard = asyncio.Lock()


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def _lock_key(thread_id: str) -> str:
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    return f"{_codex_home()}:{digest}"


async def _local_lock(key: str) -> asyncio.Lock:
    async with _local_locks_guard:
        lock = _local_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _local_locks[key] = lock
        return lock


def _lock_path(thread_id: str) -> Path:
    digest = hashlib.sha256(thread_id.encode()).hexdigest()
    return _codex_home() / ".ductor-thread-locks" / f"{digest}.lock"


@contextlib.asynccontextmanager
async def codex_thread_lease(
    thread_id: str | None,
    *,
    owner: str,
) -> AsyncIterator[None]:
    """Hold an exclusive Ductor lease while writing a known Codex thread."""
    if not thread_id:
        yield
        return

    key = _lock_key(thread_id)
    local = await _local_lock(key)
    await local.acquire()
    lock_file = None
    try:
        path = _lock_path(thread_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = path.open("a+")
        if fcntl is not None:
            await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_EX)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"owner={owner}\npid={os.getpid()}\n")
        lock_file.flush()
        yield
    finally:
        if lock_file is not None:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        local.release()
