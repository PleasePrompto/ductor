"""Workspace file reader: safe reads with fallback defaults."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


def read_file(path: Path) -> str | None:
    """Read a file, returning None if it does not exist or cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("Failed to read file: %s", path, exc_info=True)
        return None


def read_mainmemory(paths: DuctorPaths) -> str:
    """Read MAINMEMORY.md, returning empty string if missing."""
    return read_file(paths.mainmemory_path) or ""


_APPEND_FILE_MAX_BYTES = 256 * 1024


async def build_appended_files_block(paths: DuctorPaths, filenames: list[str]) -> str | None:
    """Concatenate the given *filenames* from the agent's workspace.

    Each name is resolved relative to ``paths.workspace`` and must stay inside
    it (absolute paths, ``..`` and symlinks escaping the workspace are skipped).
    Missing/empty files are skipped, as are files larger than
    ``_APPEND_FILE_MAX_BYTES`` (the block is injected into every turn's system
    prompt, so an oversized file would bloat every request). Returns ``None``
    when nothing remains so callers can leave ``append_system_prompt``
    unchanged.
    """
    parts: list[str] = []
    ws = paths.workspace.resolve()
    for fname in filenames:
        if not fname:
            continue
        target = (paths.workspace / fname).resolve()
        if not target.is_relative_to(ws):
            continue
        try:
            size = target.stat().st_size
        except OSError:
            continue
        if size > _APPEND_FILE_MAX_BYTES:
            logger.warning(
                "Skipping append_system_prompt file %s: %d bytes exceeds limit of %d",
                target,
                size,
                _APPEND_FILE_MAX_BYTES,
            )
            continue
        content = await asyncio.to_thread(read_file, target)
        if content and content.strip():
            parts.append(content.strip())
    return "\n\n".join(parts) if parts else None
