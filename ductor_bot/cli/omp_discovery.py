"""Model discovery for the Oh My Pi CLI (``omp models --json``)."""

from __future__ import annotations

import asyncio
import json
import logging
from shutil import which

from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT = 15.0


async def discover_omp_models() -> tuple[str, ...]:
    """Return model selectors reported by ``omp models --json``.

    Returns an empty tuple when the CLI is missing, times out, or errors —
    callers then fall back to the cached or hardcoded list.
    """
    binary = which("omp")
    if not binary:
        logger.debug("omp not available for model discovery")
        return ()

    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "models",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATION_FLAGS,
        )
    except (OSError, ValueError):
        logger.debug("omp models spawn failed", exc_info=True)
        return ()

    try:
        async with asyncio.timeout(_DISCOVERY_TIMEOUT):
            stdout_bytes, stderr_bytes = await proc.communicate()
    except TimeoutError:
        logger.warning("omp models discovery timed out")
        force_kill_process_tree(proc.pid)
        await proc.communicate()
        return ()

    if proc.returncode not in (0, None):
        logger.debug("omp models exited with code %s", proc.returncode)
        return ()

    output = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""
    combined = f"{output}\n{stderr}".lower()
    if any(
        token in combined
        for token in (
            "not logged in",
            "no api key",
            "unauthorized",
            "authentication",
        )
    ):
        logger.debug("omp models: not authenticated")
        return ()

    return _parse_models(output)


def _parse_models(output: str) -> tuple[str, ...]:
    """Parse ``omp models --json`` stdout into an ordered tuple of selectors."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        logger.debug("omp models: unparseable JSON")
        return ()

    if not isinstance(data, dict):
        return ()

    raw_models = data.get("models")
    if not isinstance(raw_models, list):
        return ()

    seen: set[str] = set()
    ordered: list[str] = []
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        selector = entry.get("selector")
        if not isinstance(selector, str):
            selector = entry.get("id")
        if not isinstance(selector, str):
            continue
        selector = selector.strip()
        if not selector or selector in seen:
            continue
        seen.add(selector)
        ordered.append(selector)

    return tuple(ordered)
