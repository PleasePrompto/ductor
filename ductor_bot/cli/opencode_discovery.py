"""Model discovery for the opencode CLI (``opencode models``).

Also derives the user's *default* opencode model (from the opencode config
file) and the *recently used* models (from opencode's session database). Both
sources are plain files — they work even when the ``opencode`` binary is not
on the ductor service PATH.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from shutil import which

from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT = 15.0

# opencode model IDs always take the "<provider>/<model>" form.
_MODEL_SEPARATOR = "/"

# How many recently used models to surface in the selector.
_DEFAULT_RECENT_LIMIT = 10

# Output lines that indicate no usable model list.
_AUTH_FAILURE_TOKENS = (
    "no providers",
    "no credentials",
    "sign in",
    "login required",
    "not logged in",
    "auth list",
    "auth login",
)


async def discover_opencode_models() -> tuple[str, ...]:
    """Return model IDs reported by ``opencode models``.

    Returns an empty tuple when the CLI is missing, unauthenticated, times out,
    or errors — callers then fall back to the cached list.
    """
    binary = which("opencode")
    if not binary:
        logger.debug("opencode not available for model discovery")
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
        logger.debug("opencode models spawn failed", exc_info=True)
        return ()

    try:
        async with asyncio.timeout(_DISCOVERY_TIMEOUT):
            stdout_bytes, stderr_bytes = await proc.communicate()
    except TimeoutError:
        logger.warning("opencode models discovery timed out")
        force_kill_process_tree(proc.pid)
        await proc.communicate()
        return ()

    output = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""
    combined = f"{output}\n{stderr}".lower()
    if any(token in combined for token in _AUTH_FAILURE_TOKENS):
        logger.debug("opencode models: not authenticated")
        return ()

    if proc.returncode not in (0, None):
        logger.debug("opencode models exited with code %s", proc.returncode)
        return ()

    return _parse_models(output)


def _parse_models(output: str) -> tuple[str, ...]:
    """Parse ``opencode models`` stdout into an ordered tuple of model IDs.

    Lines are bare ``<provider>/<model>`` IDs. Any line without a ``/`` is a
    header/help/usage line and is skipped; ``Usage:`` / ``Flags:`` markers stop
    parsing while keeping any model IDs collected before them.
    """
    models: list[str] = []
    seen: set[str] = set()

    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("Usage:", "Flags:", "Available subcommands:")):
            break

        if _MODEL_SEPARATOR not in line:
            continue
        model_id = line.strip().strip(",")
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append(model_id)

    return tuple(models)


# ---------------------------------------------------------------------------
# Default + recently used models (file-based, no PATH dependency)
# ---------------------------------------------------------------------------


async def discover_opencode_default_model() -> str:
    """Return the user's configured default opencode model (``""`` when unset).

    Read from ``opencode.json`` / ``opencode.jsonc`` under the opencode config
    directory (``$XDG_CONFIG_HOME/opencode``, default ``~/.config/opencode``).
    """
    return await asyncio.to_thread(_read_default_model)


async def discover_opencode_recent_models(limit: int = _DEFAULT_RECENT_LIMIT) -> tuple[str, ...]:
    """Return the most recently used opencode model IDs, newest first.

    Read from opencode's session database (``$XDG_DATA_HOME/opencode/
    opencode.db``, default ``~/.local/share/opencode/opencode.db``). Model IDs
    are normalized to the ``<provider>/<model>`` form and deduplicated, so a
    model used with different reasoning variants appears once.
    """
    return await asyncio.to_thread(_read_recent_models, limit)


def _opencode_config_dir() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return config_home / "opencode"


def _opencode_data_dir() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return data_home / "opencode"


def _read_default_model() -> str:
    """Read the top-level ``model`` key from the opencode config file."""
    config_dir = _opencode_config_dir()
    candidates = (config_dir / "opencode.json", config_dir / "opencode.jsonc")
    for path in candidates:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        model = _extract_model_key(content)
        if model:
            return model
    return ""


def _extract_model_key(content: str) -> str:
    """Return the top-level ``"model"`` string from JSON or JSONC config text."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        model = data.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    # JSONC (comments/trailing commas): fall back to a targeted regex.
    match = re.search(r'"model"\s*:\s*"([^"]+)"', content)
    if match:
        return match.group(1).strip()
    return ""


def _read_recent_models(limit: int) -> tuple[str, ...]:
    """Return the newest distinct model IDs from opencode's session database."""
    db_path = _opencode_data_dir() / "opencode.db"
    if not db_path.is_file():
        return ()
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        logger.debug("opencode session db unreadable", exc_info=True)
        return ()
    try:
        rows = con.execute(
            "SELECT model, MAX(time_updated) FROM session "
            "WHERE model IS NOT NULL GROUP BY model "
            "ORDER BY MAX(time_updated) DESC"
        ).fetchall()
    except sqlite3.Error:
        logger.debug("opencode session db query failed", exc_info=True)
        return ()
    finally:
        con.close()

    models: list[str] = []
    seen: set[str] = set()
    for raw_model, _timestamp in rows:
        model_id = _normalize_session_model(raw_model)
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append(model_id)
        if len(models) >= limit:
            break
    return tuple(models)


def _normalize_session_model(raw_model: object) -> str:
    """Normalize a session ``model`` cell into a ``<provider>/<model>`` ID.

    Newer opencode versions store a JSON object (``{"id": ..., "providerID":
    ...}``); older versions store a plain string. Variants (reasoning effort)
    are stripped because they are not part of the model ID.
    """
    if isinstance(raw_model, str) and raw_model.strip():
        try:
            parsed = json.loads(raw_model)
        except json.JSONDecodeError:
            text = raw_model.strip()
            return text if _MODEL_SEPARATOR in text else ""
    else:
        parsed = raw_model

    if not isinstance(parsed, dict):
        return ""
    model_id = parsed.get("id")
    provider_id = parsed.get("providerID")
    if not isinstance(model_id, str) or not model_id.strip():
        return ""
    if isinstance(provider_id, str) and provider_id.strip():
        return f"{provider_id.strip()}/{model_id.strip()}"
    model_id = model_id.strip()
    return model_id if _MODEL_SEPARATOR in model_id else ""
