"""SharedKnowledgeSync: watches SHAREDMEMORY.md and injects into all agents."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.infra.file_watcher import FileWatcher

if TYPE_CHECKING:
    from ductor_bot.multiagent.supervisor import AgentSupervisor

logger = logging.getLogger(__name__)

_START_MARKER = "--- SHARED KNOWLEDGE START ---"
_END_MARKER = "--- SHARED KNOWLEDGE END ---"

# Legacy HTML markers for backward compatibility (read-only).
_LEGACY_START = "<!-- SHARED:START -->"
_LEGACY_END = "<!-- SHARED:END -->"


def _find_markers(text: str) -> tuple[str, str] | None:
    """Detect which marker pair is present in *text*. Returns (start, end) or None."""
    if _START_MARKER in text:
        return _START_MARKER, _END_MARKER
    if _LEGACY_START in text:
        return _LEGACY_START, _LEGACY_END
    return None


def _sync_agent_io(shared_path: Path, mainmemory_path: Path) -> bool:
    """Synchronous file I/O for injecting shared knowledge into one agent.

    Returns True if the file was written.
    """
    if not shared_path.is_file():
        return False
    shared_content = shared_path.read_text(encoding="utf-8").strip()
    if not shared_content:
        return False
    inject_block = f"{_START_MARKER}\n{shared_content}\n{_END_MARKER}"

    if not mainmemory_path.is_file():
        return False

    current = mainmemory_path.read_text(encoding="utf-8")

    markers = _find_markers(current)
    if markers:
        start, end = markers
        before = current.split(start, 1)[0]
        after_parts = current.split(end, 1)
        after = after_parts[1] if len(after_parts) > 1 else ""
        # Always write new-format markers (migrates legacy on first sync)
        new_content = f"{before}{inject_block}{after}"
    else:
        new_content = f"{current.rstrip()}\n\n{inject_block}\n"

    if new_content != current:
        mainmemory_path.write_text(new_content, encoding="utf-8")
        return True
    return False


class SharedKnowledgeSync:
    """Watches ``SHAREDMEMORY.md`` and syncs its content into every agent's MAINMEMORY.md.

    The shared content is wrapped in Markdown-native markers::

        --- SHARED KNOWLEDGE START ---
        (content)
        --- SHARED KNOWLEDGE END ---

    Legacy HTML comment markers (``<!-- SHARED:START/END -->``) are detected
    on read and automatically migrated to the new format on write.
    """

    def __init__(self, shared_path: Path, supervisor: AgentSupervisor) -> None:
        self._path = shared_path
        self._supervisor = supervisor
        self._watcher = FileWatcher(self._path, self._on_changed)

    async def start(self) -> None:
        """Start watching and perform an initial sync.

        Creates an empty SHAREDMEMORY.md if it does not exist yet, so agents
        always have a file to write to and the FileWatcher has a target.
        """
        if not self._path.is_file():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                "# Shared Knowledge — All Agents\n\n"
                "Knowledge written here is automatically synced into every\n"
                "agent's MAINMEMORY.md by the Supervisor.\n",
                encoding="utf-8",
            )
            logger.info("Created seed SHAREDMEMORY.md at %s", self._path)
        await self._sync_all()
        await self._watcher.start()
        logger.info("SharedKnowledgeSync watching %s", self._path)

    async def stop(self) -> None:
        await self._watcher.stop()

    async def _on_changed(self) -> None:
        """FileWatcher callback — SHAREDMEMORY.md was modified."""
        logger.info("SHAREDMEMORY.md changed, syncing to all agents")
        await self._sync_all()

    async def sync_agent(self, mainmemory_path: Path) -> None:
        """Inject shared knowledge into a single agent's MAINMEMORY.md."""
        written = await asyncio.to_thread(_sync_agent_io, self._path, mainmemory_path)
        if written:
            logger.info("Synced shared knowledge to %s", mainmemory_path)

    async def _sync_all(self) -> None:
        """Inject into all registered agents' MAINMEMORY.md files."""
        for name, stack in self._supervisor.stacks.items():
            try:
                await self.sync_agent(stack.paths.mainmemory_path)
            except Exception:
                logger.exception("Failed to sync shared knowledge to agent '%s'", name)
