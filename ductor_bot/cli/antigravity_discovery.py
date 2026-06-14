"""Model discovery for the Antigravity CLI (agy)."""

from __future__ import annotations

import asyncio
import logging

from ductor_bot.cli.antigravity_runtime import antigravity_process_env
from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT = 15.0


async def discover_antigravity_models() -> tuple[str, ...]:
    """Return the model display names reported by ``agy models``.

    ``agy models`` prints one model per line (e.g. ``Gemini 3.5 Flash
    (Medium)``); each display name is a valid ``--model`` value. Returns an
    empty tuple when agy is missing, unauthenticated, times out, or errors —
    callers then fall back to the cached or hardcoded list.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "agy",
            "models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=antigravity_process_env(),
            creationflags=_CREATION_FLAGS,
        )
    except (OSError, ValueError):
        logger.debug("agy not available for model discovery", exc_info=True)
        return ()

    try:
        async with asyncio.timeout(_DISCOVERY_TIMEOUT):
            stdout_bytes, _ = await proc.communicate()
    except TimeoutError:
        logger.warning("agy models discovery timed out")
        force_kill_process_tree(proc.pid)
        await proc.communicate()
        return ()

    if proc.returncode != 0:
        logger.debug("agy models exited with code %s", proc.returncode)
        return ()

    return _parse_models(stdout_bytes.decode(errors="replace"))


def _parse_models(output: str) -> tuple[str, ...]:
    """Parse ``agy models`` stdout into a tuple of model display names."""
    models: list[str] = []
    for raw in output.splitlines():
        name = raw.strip()
        if not name:
            continue
        # A usage/help banner means the command was rejected — treat as failure.
        if name.startswith(("Usage:", "Flags:", "Available subcommands:")):
            return ()
        models.append(name)
    return tuple(models)
