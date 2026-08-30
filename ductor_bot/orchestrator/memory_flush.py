"""Pre-compaction silent memory flush + LLM-driven compaction (#77, #80).

When the CLI emits ``CompactBoundaryEvent`` mid-stream, this helper runs a
silent follow-up turn that instructs the agent to APPEND durable facts to
``memory_system/MAINMEMORY.md`` so the post-compaction context retains what
the user just told us (#77). When the resulting file grows past
``trigger_lines``, a second silent turn chains in to rewrite the file
densely -- preserving recent entries verbatim and compressing older
clusters into one dense semantic entry each (#80).

Design notes:
- Boundary detection is additive: unsubscribed callers see no change.
- Dedup is in-memory (``dict[SessionKey, float]`` of monotonic timestamps).
  Process restart is a natural reset; a duplicate flush would cost at most
  one extra CLI call, never corrupt memory.
- Errors during flush or compaction are logged at WARNING and swallowed.
  Memory maintenance must never delay the user turn.
"""

from __future__ import annotations

import contextlib
import logging
import time
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from ductor_bot.cli.types import AgentRequest
from ductor_bot.errors import CLIError
from ductor_bot.handoff.paths import handoff_file
from ductor_bot.handoff.prompts import consolidation_prompt
from ductor_bot.handoff.reinject import ReinjectFlags
from ductor_bot.workspace.loader import read_mainmemory

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ductor_bot.bus.lock_pool import LockPool
    from ductor_bot.cli.service import CLIService
    from ductor_bot.config import MemoryCompactionConfig, MemoryFlushConfig
    from ductor_bot.session import SessionKey
    from ductor_bot.session.manager import SessionData
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


class MemoryFlusher:
    """Tracks pre-compaction boundary events and runs silent flush + compact turns."""

    def __init__(
        self,
        config: MemoryFlushConfig,
        cli_service: CLIService,
        compaction_config: MemoryCompactionConfig,
        paths: DuctorPaths,
        *,
        lock_pool: LockPool | None = None,
    ) -> None:
        self._config = config
        self._cli = cli_service
        self._compaction = compaction_config
        self._paths = paths
        self._lock_pool = lock_pool
        self._reinject: ReinjectFlags | None = None
        self._folder_resolver: Callable[[SessionKey], Path | None] | None = None
        self._boundary_seen: set[SessionKey] = set()
        self._last_flushed: dict[SessionKey, float] = {}

    def set_folder_resolver(self, resolver: Callable[[SessionKey], Path | None]) -> None:
        """Attach the binding lookup, so the flush turn can name the handoff."""
        self._folder_resolver = resolver

    def _consolidation_for(self, key: SessionKey) -> str:
        """The consolidation instruction, naming this conversation's handoff."""
        folder = self._folder_resolver(key) if self._folder_resolver else None
        return consolidation_prompt(handoff_file(key, folder, self._paths))

    def set_reinject(self, reinject: ReinjectFlags) -> None:
        """Attach the handoff re-injection flags (late wiring, like the lock pool)."""
        self._reinject = reinject

    def set_lock_pool(self, lock_pool: LockPool) -> None:
        """Attach the shared ``LockPool`` after construction (late wiring)."""
        self._lock_pool = lock_pool

    def _session_lock(self, key: SessionKey) -> AbstractAsyncContextManager[object]:
        """Return an async context manager for the per-session lock.

        Falls back to ``nullcontext`` when no ``LockPool`` is attached so
        ``MemoryFlusher`` remains usable unlocked (e.g. in unit tests).
        """
        if self._lock_pool is None:
            return contextlib.nullcontext()
        return self._lock_pool.get(key.lock_key)

    def mark_boundary(self, key: SessionKey) -> None:
        """Record that a CompactBoundaryEvent was seen for this session.

        The same boundary owes the conversation a handoff re-injection: the
        session id does not change across a compaction, so nothing else would
        notice that the model has just lost its working context.
        """
        self._boundary_seen.add(key)
        if self._reinject is not None:
            self._reinject.mark(key)
        logger.debug("Memory flush: boundary marked chat=%d", key.chat_id)

    def should_flush(self, key: SessionKey) -> bool:
        """True when a flush is due for this session key."""
        if key not in self._boundary_seen:
            return False
        last = self._last_flushed.get(key)
        if last is None:
            return True
        return (time.monotonic() - last) > self._config.dedup_seconds

    def should_compact(self) -> bool:
        """True when compaction is enabled and MAINMEMORY.md exceeds threshold."""
        if not self._compaction.enabled:
            return False
        content = read_mainmemory(self._paths)
        line_count = len(content.splitlines())
        return line_count >= self._compaction.trigger_lines

    async def maybe_flush(self, key: SessionKey, session: SessionData) -> None:
        """Run the silent flush turn if due, and compaction if file is large."""
        if not self.should_flush(key):
            return
        await self.flush(key, session)
        if self.should_compact():
            await self.compact(key, session)

    async def flush(self, key: SessionKey, session: SessionData) -> None:
        """Run a silent flush turn resuming the current session."""
        session_id = session.session_id
        if not session_id:
            logger.debug("Memory flush skipped chat=%d: no resume session_id", key.chat_id)
            self._boundary_seen.discard(key)
            return

        request = AgentRequest(
            prompt=f"{self._config.flush_prompt}\n{self._consolidation_for(key)}",
            chat_id=key.chat_id,
            topic_id=key.topic_id,
            transport=key.transport,
            resume_session=session_id,
            process_label="memory_flush",
        )
        logger.info("Memory flush firing chat=%d session=%s", key.chat_id, session_id[:8])
        try:
            async with self._session_lock(key):
                await self._cli.execute(request)
        except (CLIError, RuntimeError, OSError) as exc:
            logger.warning("Memory flush failed chat=%d: %s", key.chat_id, exc)
        finally:
            self._last_flushed[key] = time.monotonic()
            self._boundary_seen.discard(key)

    async def compact(self, key: SessionKey, session: SessionData) -> None:
        """Run a silent compaction turn resuming the current session."""
        session_id = session.session_id
        if not session_id:
            logger.debug(
                "Memory compaction skipped chat=%d: no resume session_id",
                key.chat_id,
            )
            return

        prompt = self._render_compact_prompt()
        request = AgentRequest(
            prompt=prompt,
            chat_id=key.chat_id,
            topic_id=key.topic_id,
            transport=key.transport,
            resume_session=session_id,
            process_label="memory_compact",
        )
        logger.info(
            "Memory compaction firing chat=%d session=%s",
            key.chat_id,
            session_id[:8],
        )
        try:
            async with self._session_lock(key):
                await self._cli.execute(request)
        except (CLIError, RuntimeError, OSError) as exc:
            logger.warning("Memory compaction failed chat=%d: %s", key.chat_id, exc)

    def _render_compact_prompt(self) -> str:
        """Render the compaction prompt, falling back to the default on typo errors.

        A user-configured template with an unknown ``{placeholder}`` (e.g. a
        typo like ``{preserv_days}``) must never suppress the real user turn.
        """
        fmt_kwargs = {
            "target_lines": self._compaction.target_lines,
            "preserve_days": self._compaction.preserve_recency_days,
        }
        try:
            return self._compaction.prompt.format(**fmt_kwargs)
        except (KeyError, IndexError) as exc:
            # Import locally: MemoryCompactionConfig is TYPE_CHECKING-only at module scope.
            from ductor_bot.config import MemoryCompactionConfig

            logger.warning(
                "Memory compaction prompt template has invalid placeholder (%s); "
                "falling back to default template.",
                exc,
            )
            default_prompt = MemoryCompactionConfig.model_fields["prompt"].default
            assert isinstance(default_prompt, str)
            return default_prompt.format(**fmt_kwargs)
