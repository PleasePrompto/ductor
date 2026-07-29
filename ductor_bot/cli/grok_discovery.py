"""Model + per-model reasoning-effort discovery for the xAI Grok Build CLI."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from shutil import which

from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT = 15.0
_EFFORT_PROBE_TIMEOUT = 8.0

# Lines under "Available models:" look like:
#   * grok-4.5 (default)
#   - grok-composer-2.5-fast
_MODEL_BULLET = re.compile(r"^\s*[\*\-•]\s+(\S+)")
_DEFAULT_LINE = re.compile(r"(?i)^\s*Default model:\s*(\S+)")

# Grok rejects unknown effort levels with:
#   unknown effort level 'X'; use one of: high, medium, low
_USE_ONE_OF = re.compile(r"use one of:\s*([^\n\"'}]+)", re.IGNORECASE)

# Canonical display / button order (CLI vocabulary is a superset of any one model).
_EFFORT_ORDER: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

# Probe token that no model menu should ever advertise.
_PROBE_EFFORT = "__ductor_probe__"

# Conservative default when a probe fails. Matches current grok-4.5 menu.
_FALLBACK_EFFORTS: tuple[str, ...] = ("low", "medium", "high")


@dataclass(frozen=True, slots=True)
class GrokModelInfo:
    """A Grok Build model with the reasoning-effort levels its menu accepts."""

    id: str
    supported_efforts: tuple[str, ...]
    default_effort: str


async def discover_grok_models() -> list[GrokModelInfo]:
    """Return models reported by ``grok models``, each with probed efforts.

    Returns an empty list when the CLI is missing, unauthenticated, times out,
    or errors — callers then fall back to the cached or hardcoded list.

    Effort menus are **per model**. The CLI documents a full vocabulary
    (none/minimal/low/.../max) but only accepts levels the active model
    advertises; probing with an invalid level returns that menu without
    starting a turn.
    """
    model_ids = await _discover_model_ids()
    if not model_ids:
        return []

    results = await asyncio.gather(
        *(_probe_model_efforts(model_id) for model_id in model_ids),
        return_exceptions=True,
    )

    models: list[GrokModelInfo] = []
    for model_id, result in zip(model_ids, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "Grok effort probe failed for %s; using fallback levels",
                model_id,
                exc_info=result,
            )
            efforts = _FALLBACK_EFFORTS
        else:
            efforts = result or _FALLBACK_EFFORTS
        models.append(
            GrokModelInfo(
                id=model_id,
                supported_efforts=efforts,
                default_effort=_pick_default_effort(efforts),
            )
        )

    logger.info(
        "Grok discovery found %d models: %s",
        len(models),
        ", ".join(f"{m.id}[{'/'.join(m.supported_efforts)}]" for m in models),
    )
    return models


async def _discover_model_ids() -> tuple[str, ...]:
    """Return model IDs from ``grok models`` (no effort probing)."""
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


async def _probe_model_efforts(model_id: str) -> tuple[str, ...]:
    """Probe the effort menu for *model_id* via an intentional invalid level."""
    binary = which("grok")
    if not binary:
        return ()

    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
            "--always-approve",
            "--model",
            model_id,
            "--reasoning-effort",
            _PROBE_EFFORT,
            "-p",
            "x",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATION_FLAGS,
        )
    except (OSError, ValueError):
        logger.debug("grok effort probe spawn failed for %s", model_id, exc_info=True)
        return ()

    try:
        async with asyncio.timeout(_EFFORT_PROBE_TIMEOUT):
            stdout_bytes, stderr_bytes = await proc.communicate()
    except TimeoutError:
        logger.warning("grok effort probe timed out for %s", model_id)
        force_kill_process_tree(proc.pid)
        await proc.communicate()
        return ()

    text = (
        stdout_bytes.decode(errors="replace")
        + "\n"
        + (stderr_bytes.decode(errors="replace") if stderr_bytes else "")
    )
    efforts = _parse_supported_efforts(text)
    if efforts:
        return efforts

    logger.debug(
        "Grok effort probe for %s returned no parseable menu (rc=%s)",
        model_id,
        proc.returncode,
    )
    return ()


def _parse_supported_efforts(text: str) -> tuple[str, ...]:
    """Extract effort levels from a Grok CLI error / help string."""
    match = _USE_ONE_OF.search(text)
    if not match:
        return ()
    raw_parts = [p.strip().strip(".,;\"'") for p in match.group(1).split(",")]
    parts = [p for p in raw_parts if p and p != _PROBE_EFFORT]
    return order_efforts(parts)


def order_efforts(efforts: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Order effort labels into the canonical menu sequence."""
    seen: set[str] = set()
    ordered: list[str] = []
    for level in _EFFORT_ORDER:
        if level in efforts and level not in seen:
            ordered.append(level)
            seen.add(level)
    for level in efforts:
        if level not in seen:
            ordered.append(level)
            seen.add(level)
    return tuple(ordered)


def _pick_default_effort(efforts: tuple[str, ...]) -> str:
    """Pick a sensible default from a model's supported set."""
    for preferred in ("medium", "low", "high"):
        if preferred in efforts:
            return preferred
    return efforts[0] if efforts else "medium"


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
