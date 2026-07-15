"""Model discovery for the xAI Grok Build CLI (``grok models``)."""

from __future__ import annotations

import asyncio
import logging
import re
from shutil import which

from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT = 15.0

# Lines under "Available models:" look like:
#   * grok-4.5 (default)
#   - grok-composer-2.5-fast
_MODEL_BULLET = re.compile(r"^\s*[\*\-•]\s+(\S+)")
_DEFAULT_LINE = re.compile(r"(?i)^\s*Default model:\s*(\S+)")


async def discover_grok_models() -> tuple[str, ...]:
    """Return model IDs reported by ``grok models``.

    Returns an empty tuple when the CLI is missing, unauthenticated, times out,
    or errors — callers then fall back to the cached or hardcoded list.
    """
    binary = which("grok")
    if not binary:
        logger.debug("grok not available for model discovery")
        return ()

    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATION_FLAGS,
        )
    except (OSError, ValueError):
        logger.debug("grok models spawn failed", exc_info=True)
        return ()

    try:
        async with asyncio.timeout(_DISCOVERY_TIMEOUT):
            stdout_bytes, stderr_bytes = await proc.communicate()
    except TimeoutError:
        logger.warning("grok models discovery timed out")
        force_kill_process_tree(proc.pid)
        await proc.communicate()
        return ()

    output = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""
    combined = f"{output}\n{stderr}".lower()
    if any(
        token in combined
        for token in (
            "not logged in",
            "sign in",
            "login required",
            "unauthorized",
            "run `grok login`",
        )
    ):
        logger.debug("grok models: not authenticated")
        return ()

    if proc.returncode not in (0, None):
        logger.debug("grok models exited with code %s", proc.returncode)
        return ()

    return _parse_models(output)


def _parse_models(output: str) -> tuple[str, ...]:
    """Parse ``grok models`` stdout into an ordered tuple of model IDs.

    Bullet lines under Available models take precedence. If none are found,
    fall back to a ``Default model:`` line so a single-model install still
    populates the cache.
    """
    models: list[str] = []
    seen: set[str] = set()
    default_model: str | None = None

    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("Usage:", "Flags:", "Available subcommands:")):
            return ()

        default_match = _DEFAULT_LINE.match(line)
        if default_match:
            default_model = _normalize_model_id(default_match.group(1))
            continue

        bullet = _MODEL_BULLET.match(line)
        if not bullet:
            continue
        model_id = _normalize_model_id(bullet.group(1))
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append(model_id)

    if models:
        return tuple(models)

    if default_model:
        return (default_model,)

    return ()


def _normalize_model_id(raw: str) -> str:
    """Strip decoration such as trailing ``(default)`` from a model token."""
    token = raw.strip().strip(",")
    # Drop parenthetical suffix glued without space: rare, keep first token only.
    if "(" in token:
        token = token.split("(", 1)[0].strip()
    return token
