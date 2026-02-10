"""Interactive file browser for the ~/.ductor directory.

Renders the ductor home directory as a navigable inline-keyboard tree.
Folders are clickable buttons that edit the message in-place; files are
listed in the text body for reference.

Callback data encoding (must fit 64 bytes):
    ``sf:<rel_path>``  -- navigate to directory (empty = root)
    ``sf!<rel_path>``  -- request files from AI agent
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.bot.response_format import SEP, fmt
from ductor_bot.security.paths import is_path_safe

if TYPE_CHECKING:
    from ductor_bot.workspace.paths import DuctorPaths

SF_PREFIX = "sf:"
SF_FILE_PREFIX = "sf!"

_EXCLUDED_NAMES = frozenset({"__pycache__", ".git"})
_MAX_BUTTONS_PER_ROW = 3


def is_file_browser_callback(data: str) -> bool:
    """Return True if *data* belongs to the file browser."""
    return data.startswith((SF_PREFIX, SF_FILE_PREFIX))


async def file_browser_start(paths: DuctorPaths) -> tuple[str, InlineKeyboardMarkup]:
    """Build the initial ``/showfiles`` response for the root directory."""
    return await asyncio.to_thread(_build_view, paths, "")


async def handle_file_browser_callback(
    paths: DuctorPaths,
    data: str,
) -> tuple[str, InlineKeyboardMarkup | None, str | None]:
    """Route a ``sf:`` or ``sf!`` callback.

    Returns ``(text, keyboard, agent_prompt)``.  *agent_prompt* is set only
    for ``sf!`` file-request callbacks; the caller should feed it to the
    orchestrator as a normal message.
    """
    if data.startswith(SF_FILE_PREFIX):
        rel = data[len(SF_FILE_PREFIX) :]
        abs_dir = (paths.ductor_home / rel).resolve() if rel else paths.ductor_home.resolve()
        prompt = (
            f"List all files in {abs_dir}/ and send me whichever one I ask for. "
            "Deliver files using file tags."
        )
        return "", None, prompt

    rel = data[len(SF_PREFIX) :]
    text, keyboard = await asyncio.to_thread(_build_view, paths, rel)
    return text, keyboard, None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_view(paths: DuctorPaths, rel: str) -> tuple[str, InlineKeyboardMarkup]:
    """Build the text + keyboard for a directory listing."""
    base = paths.ductor_home.resolve()
    target = (base / rel).resolve() if rel else base

    if not is_path_safe(target, [base]) or not target.is_dir():
        return fmt("**File Browser**", SEP, "Directory not found."), InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="<< Back", callback_data="sf:")]]
        )

    dirs, files = _list_directory(target)

    display_path = f"~/.ductor/{rel}" if rel else "~/.ductor/"
    if not display_path.endswith("/"):
        display_path += "/"

    body_lines = [f"  {d}/" for d in dirs]
    body_lines.extend(f"  {f}" for f in files)

    if not body_lines:
        body_lines.append("  (empty)")

    text = fmt("**File Browser**", SEP, f"`{display_path}`\n\n" + "\n".join(body_lines), SEP)

    # Build keyboard: folder buttons in rows of _MAX_BUTTONS_PER_ROW
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for d in dirs:
        child_rel = f"{rel}/{d}" if rel else d
        row.append(InlineKeyboardButton(text=f"{d}/", callback_data=f"sf:{child_rel}"))
        if len(row) >= _MAX_BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Back button (not at root)
    if rel:
        parent = str(Path(rel).parent)
        parent_cb = "sf:" if parent == "." else f"sf:{parent}"
        rows.append([InlineKeyboardButton(text="<< Back", callback_data=parent_cb)])

    # File request button
    rows.append(
        [InlineKeyboardButton(text="Send me a file from this folder", callback_data=f"sf!{rel}")]
    )

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _list_directory(target: Path) -> tuple[list[str], list[str]]:
    """List directory contents, returning ``(dirs, files)`` sorted alphabetically.

    Hidden files and excluded directories are filtered out.
    """
    dirs: list[str] = []
    files: list[str] = []
    try:
        for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            name = entry.name
            if name.startswith(".") or name in _EXCLUDED_NAMES:
                continue
            if entry.is_dir():
                dirs.append(name)
            else:
                files.append(name)
    except PermissionError:
        pass
    return dirs, files
