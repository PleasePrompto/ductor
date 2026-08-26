"""Interactive file browser.

Shows ``~/.ductor`` alongside every directory configured in ``project_roots``,
so the folders an agent actually works in are reachable from Telegram rather
than only its own workspace.

Paths are addressed by token, not by relative path: ``callback_data`` is capped
at 64 bytes and a few levels of nesting used to overflow it silently.

Callback data:
    ``sf:``            -- root list
    ``sf:<token>``     -- open that directory
    ``sf!<token>``     -- send that file
    ``sf@<token>``     -- send that directory as a zip
    ``sf?<token>``     -- ask the agent about that directory
"""

from __future__ import annotations

import asyncio
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.files.browser import list_directory
from ductor_bot.files.path_tokens import path_for, token_for
from ductor_bot.files.roots import browsable_roots, contains, label_for
from ductor_bot.i18n import t
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ductor_bot.workspace.paths import DuctorPaths

SF_PREFIX = "sf:"
SF_FILE_PREFIX = "sf!"
SF_ZIP_PREFIX = "sf@"
SF_ASK_PREFIX = "sf?"

_MAX_BUTTONS_PER_ROW = 3
#: Telegram refuses bot uploads past 50 MB; stop before building the archive
#: rather than after spending the time and disk.
_MAX_SEND_BYTES = 45 * 1024 * 1024
_MAX_ZIP_FILES = 2000


@dataclass(frozen=True, slots=True)
class BrowserAction:
    """What the transport should do after a callback."""

    text: str = ""
    keyboard: InlineKeyboardMarkup | None = None
    send_path: Path | None = None
    zip_dir: Path | None = None
    agent_prompt: str | None = None


def is_file_browser_callback(data: str) -> bool:
    """Return True if *data* belongs to the file browser."""
    return data.startswith((SF_PREFIX, SF_FILE_PREFIX, SF_ZIP_PREFIX, SF_ASK_PREFIX))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def file_browser_start(
    paths: DuctorPaths, project_roots: Mapping[str, str]
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the ``/showfiles`` root view."""
    return await asyncio.to_thread(_build_root_view, paths, project_roots)


async def handle_file_browser_callback(
    paths: DuctorPaths,
    project_roots: Mapping[str, str],
    data: str,
) -> BrowserAction:
    """Route an ``sf`` callback."""
    return await asyncio.to_thread(_handle, paths, project_roots, data)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _parse(data: str) -> tuple[str, str] | None:
    """Split callback data into ``(prefix, token)``."""
    for prefix in (SF_FILE_PREFIX, SF_ZIP_PREFIX, SF_ASK_PREFIX, SF_PREFIX):
        if data.startswith(prefix):
            return prefix, data[len(prefix) :]
    return None


def _send_file_action(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path
) -> BrowserAction:
    """Send a file, or explain that it is past Telegram's upload ceiling."""
    if not target.is_file():
        return _root_action(paths, project_roots)
    size = target.stat().st_size
    if size <= _MAX_SEND_BYTES:
        return BrowserAction(send_path=target)
    text, kb = _build_dir_view(paths, project_roots, target.parent)
    note = t("file_browser.too_large", name=target.name, mb=size // 1048576)
    return BrowserAction(text=f"{text}\n\n{note}", keyboard=kb)


def _ask_action(_paths: DuctorPaths, _roots: Mapping[str, str], target: Path) -> BrowserAction:
    return BrowserAction(agent_prompt=t("file_browser.file_request_prompt", dir=target))


def _zip_action(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path
) -> BrowserAction:
    if not target.is_dir():
        return _root_action(paths, project_roots)
    return BrowserAction(zip_dir=target)


def _open_action(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path
) -> BrowserAction:
    if not target.is_dir():
        return _root_action(paths, project_roots)
    text, kb = _build_dir_view(paths, project_roots, target)
    return BrowserAction(text=text, keyboard=kb)


_ACTIONS = {
    SF_ASK_PREFIX: _ask_action,
    SF_FILE_PREFIX: _send_file_action,
    SF_ZIP_PREFIX: _zip_action,
    SF_PREFIX: _open_action,
}


def _handle(paths: DuctorPaths, project_roots: Mapping[str, str], data: str) -> BrowserAction:
    parsed = _parse(data)
    if parsed is None or not parsed[1]:
        return _root_action(paths, project_roots)
    prefix, token = parsed

    target = path_for(token)
    # An unknown token means the entry was evicted or the bot restarted since
    # the message was sent. Falling back to the root beats an error the user
    # can do nothing about.
    roots = browsable_roots(paths.ductor_home, project_roots)
    if target is None or not contains(roots, target):
        return _root_action(paths, project_roots)

    return _ACTIONS[prefix](paths, project_roots, target)


def _root_action(paths: DuctorPaths, project_roots: Mapping[str, str]) -> BrowserAction:
    text, kb = _build_root_view(paths, project_roots)
    return BrowserAction(text=text, keyboard=kb)


def _build_root_view(
    paths: DuctorPaths, project_roots: Mapping[str, str]
) -> tuple[str, InlineKeyboardMarkup]:
    roots = browsable_roots(paths.ductor_home, project_roots)

    if not roots:
        return fmt(t("file_browser.header"), SEP, t("file_browser.no_roots")), InlineKeyboardMarkup(
            inline_keyboard=[]
        )

    body = "\n".join(f"  {label}/" for label in roots)
    text = fmt(t("file_browser.header"), SEP, f"{t('file_browser.pick_root')}\n\n{body}", SEP)

    buttons = [
        InlineKeyboardButton(text=f"{label}/", callback_data=f"{SF_PREFIX}{token_for(path)}")
        for label, path in roots.items()
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=_rows(buttons))


def _build_dir_view(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path
) -> tuple[str, InlineKeyboardMarkup]:
    roots = browsable_roots(paths.ductor_home, project_roots)
    owner = label_for(roots, target)
    if owner is None:
        return _build_root_view(paths, project_roots)
    label, root = owner

    dirs, files = list_directory(target)
    rel = target.relative_to(root)
    display = f"{label}/{rel}" if str(rel) != "." else f"{label}/"

    lines = [f"  {d}/" for d in dirs]
    lines += [f"  {f}" for f in files]
    if not lines:
        lines.append(f"  {t('file_browser.empty')}")

    text = fmt(
        t("file_browser.header"),
        SEP,
        f"`{display}`\n\n" + "\n".join(lines),
        SEP,
        t("file_browser.tap_hint"),
    )

    buttons = [
        InlineKeyboardButton(text=f"{d}/", callback_data=f"{SF_PREFIX}{token_for(target / d)}")
        for d in dirs
    ]
    buttons += [
        InlineKeyboardButton(
            text=f"📄 {f}", callback_data=f"{SF_FILE_PREFIX}{token_for(target / f)}"
        )
        for f in files
    ]
    rows = _rows(buttons)

    nav: list[InlineKeyboardButton] = []
    if target != root:
        nav.append(
            InlineKeyboardButton(
                text=t("file_browser.btn_up"),
                callback_data=f"{SF_PREFIX}{token_for(target.parent)}",
            )
        )
    nav.append(InlineKeyboardButton(text=t("file_browser.btn_roots"), callback_data=SF_PREFIX))
    rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                text=t("file_browser.btn_zip"),
                callback_data=f"{SF_ZIP_PREFIX}{token_for(target)}",
            ),
            InlineKeyboardButton(
                text=t("file_browser.btn_ask"),
                callback_data=f"{SF_ASK_PREFIX}{token_for(target)}",
            ),
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _rows(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [
        buttons[i : i + _MAX_BUTTONS_PER_ROW] for i in range(0, len(buttons), _MAX_BUTTONS_PER_ROW)
    ]


def build_zip(directory: Path) -> tuple[Path | None, str]:
    """Zip *directory* for sending. Returns ``(archive, error_key)``.

    Hidden entries are skipped, matching what the browser displays: a listing
    that omits ``.git`` should not produce an archive containing it.
    """
    total = 0
    members: list[Path] = []
    for item in sorted(directory.rglob("*")):
        if any(part.startswith(".") for part in item.relative_to(directory).parts):
            continue
        if not item.is_file():
            continue
        members.append(item)
        try:
            total += item.stat().st_size
        except OSError:
            continue
        if total > _MAX_SEND_BYTES or len(members) > _MAX_ZIP_FILES:
            return None, "file_browser.zip_too_large"

    if not members:
        return None, "file_browser.empty"

    out = Path(mkdtemp(prefix="ductor_zip_")) / f"{directory.name or 'archive'}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in members:
            zf.write(item, item.relative_to(directory))

    if out.stat().st_size > _MAX_SEND_BYTES:
        out.unlink(missing_ok=True)
        return None, "file_browser.zip_too_large"
    return out, ""
