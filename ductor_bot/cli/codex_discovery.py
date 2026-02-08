"""Dynamic Codex model discovery via ``codex app-server`` JSON-RPC."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from shutil import which

logger = logging.getLogger(__name__)

_INIT_MSG = json.dumps(
    {
        "jsonrpc": "2.0",
        "method": "initialize",
        "id": 1,
        "params": {"clientInfo": {"name": "ductor", "version": "1.0"}},
    }
)
_LIST_MSG = json.dumps(
    {
        "jsonrpc": "2.0",
        "method": "model/list",
        "id": 2,
        "params": {},
    }
)
_INPUT = f"{_INIT_MSG}\n{_LIST_MSG}\n"


@dataclass(frozen=True, slots=True)
class CodexModelInfo:
    """A model discovered from the Codex app-server."""

    id: str
    display_name: str
    description: str
    supported_efforts: tuple[str, ...]
    default_effort: str
    is_default: bool


async def discover_codex_models(*, deadline: float = 10.0) -> list[CodexModelInfo]:
    """Query ``codex app-server`` for available models.

    Returns an empty list on timeout, missing CLI, or parse error.
    Never raises -- all errors are logged and swallowed.
    """
    codex_path = which("codex")
    if not codex_path:
        logger.debug("codex CLI not found, skipping model discovery")
        return []

    try:
        process = await asyncio.create_subprocess_exec(
            codex_path,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(deadline):
            stdout, _ = await process.communicate(input=_INPUT.encode())
    except TimeoutError:
        logger.warning("Codex discovery timeout after %.0fs", deadline)
        return _kill_process(process)
    except OSError:
        logger.warning("Failed to spawn codex app-server", exc_info=True)
        return []

    models = _parse_response(stdout.decode(errors="replace"))
    logger.info("Codex discovery found %d models", len(models))
    return models


def _kill_process(process: asyncio.subprocess.Process) -> list[CodexModelInfo]:
    """Best-effort kill of a hung process."""
    with contextlib.suppress(OSError):
        process.kill()
    return []


def _parse_response(raw: str) -> list[CodexModelInfo]:
    """Parse JSON-RPC stdout lines for the model/list response."""
    for line in raw.strip().splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") != 2:
            continue
        data = msg.get("result", {}).get("data", [])
        return [_parse_model(m) for m in data if isinstance(m, dict)]

    logger.warning("No model/list response found in codex app-server output")
    return []


def _parse_model(entry: dict[str, object]) -> CodexModelInfo:
    """Parse a single model entry from the JSON-RPC response."""
    efforts_raw = entry.get("supportedReasoningEfforts", [])
    efforts = tuple(
        e["reasoningEffort"]
        for e in (efforts_raw if isinstance(efforts_raw, list) else [])
        if isinstance(e, dict) and "reasoningEffort" in e
    )
    return CodexModelInfo(
        id=str(entry.get("id", "")),
        display_name=str(entry.get("displayName", "")),
        description=str(entry.get("description", "")),
        supported_efforts=efforts or ("medium",),
        default_effort=str(entry.get("defaultReasoningEffort", "medium")),
        is_default=bool(entry.get("isDefault", False)),
    )
