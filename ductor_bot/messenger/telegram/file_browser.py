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
from ductor_bot.files.git_status import (
    pending_commits,
    read_state,
)
from ductor_bot.files.git_status import (
    pull as git_pull,
)
from ductor_bot.files.git_status import (
    push as git_push,
)
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
SF_PULL_PREFIX = "sf<"
SF_PUSH_PREFIX = "sf>"
#: Push is confirmed before it runs: the tap that authorises it should be made
#: against a list of what will be published, not a bare count.
SF_PUSH_CONFIRM_PREFIX = "sf!!"

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
    return data.startswith(
        (
            SF_PREFIX,
            SF_FILE_PREFIX,
            SF_ZIP_PREFIX,
            SF_ASK_PREFIX,
            SF_PULL_PREFIX,
            SF_PUSH_PREFIX,
            SF_PUSH_CONFIRM_PREFIX,
        )
    )


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
    # SF_PUSH_CONFIRM_PREFIX starts with SF_FILE_PREFIX, so it must be tried first.
    for prefix in (
        SF_PUSH_CONFIRM_PREFIX,
        SF_FILE_PREFIX,
        SF_ZIP_PREFIX,
        SF_ASK_PREFIX,
        SF_PULL_PREFIX,
        SF_PUSH_PREFIX,
        SF_PREFIX,
    ):
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


def _pull_action(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path
) -> BrowserAction:
    state = read_state(target)
    if state is None or not state.has_upstream:
        return _open_action(paths, project_roots, target)
    ok, output = git_pull(state)
    key = "file_browser.pull_ok" if ok else "file_browser.pull_failed"
    return _with_notice(paths, project_roots, target, t(key, output=_trim(output)))


def _push_action(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path
) -> BrowserAction:
    """Show what a push would publish and ask for confirmation."""
    state = read_state(target)
    if state is None or not state.has_upstream:
        return _open_action(paths, project_roots, target)
    if not state.can_push:
        return _with_notice(paths, project_roots, target, t("file_browser.push_nothing"))

    commits = "\n".join(f"  {line}" for line in pending_commits(state))
    text, _ = _build_dir_view(paths, project_roots, target)
    confirm = [
        InlineKeyboardButton(
            text=t("file_browser.btn_push_confirm", count=state.ahead),
            callback_data=f"{SF_PUSH_CONFIRM_PREFIX}{token_for(target)}",
        ),
        InlineKeyboardButton(
            text=t("file_browser.btn_cancel"),
            callback_data=f"{SF_PREFIX}{token_for(target)}",
        ),
    ]
    body = t("file_browser.push_confirm", branch=state.branch, count=state.ahead)
    return BrowserAction(
        text=f"{text}\n\n{body}\n{commits}",
        keyboard=InlineKeyboardMarkup(inline_keyboard=[confirm]),
    )


def _push_confirmed_action(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path
) -> BrowserAction:
    state = read_state(target)
    if state is None or not state.can_push:
        return _open_action(paths, project_roots, target)
    ok, output = git_push(state)
    key = "file_browser.push_ok" if ok else "file_browser.push_failed"
    return _with_notice(paths, project_roots, target, t(key, output=_trim(output)))


def _with_notice(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path, notice: str
) -> BrowserAction:
    """Re-render the directory with a result line appended."""
    text, kb = _build_dir_view(paths, project_roots, target)
    return BrowserAction(text=f"{text}\n\n{notice}", keyboard=kb)


def _trim(output: str, limit: int = 400) -> str:
    output = output.strip()
    return output if len(output) <= limit else output[: limit - 1] + "…"


_ACTIONS = {
    SF_ASK_PREFIX: _ask_action,
    SF_FILE_PREFIX: _send_file_action,
    SF_ZIP_PREFIX: _zip_action,
    SF_PREFIX: _open_action,
    SF_PULL_PREFIX: _pull_action,
    SF_PUSH_PREFIX: _push_action,
    SF_PUSH_CONFIRM_PREFIX: _push_confirmed_action,
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

    # Both controls are always present. At a root they happen to lead to the
    # same place, and that redundancy is the cheaper trade: a button that
    # appears and disappears depending on depth reads as a broken screen, while
    # a stable row is predictable to tap without looking.
    at_root = target == root
    back_target = SF_PREFIX if at_root else f"{SF_PREFIX}{token_for(target.parent)}"
    rows.append(
        [
            InlineKeyboardButton(text=t("file_browser.btn_back"), callback_data=back_target),
            InlineKeyboardButton(text=t("file_browser.btn_home"), callback_data=SF_PREFIX),
        ]
    )
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

    git_row, git_line = _git_row(target)
    if git_row:
        rows.append(git_row)
        text = f"{text}\n{git_line}"

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _git_row(target: Path) -> tuple[list[InlineKeyboardButton], str]:
    """Pull/push controls for the repository containing *target*.

    Telegram has no disabled button, so an unavailable action is rendered with
    a label saying so and a tap that explains rather than acts. Hiding it
    instead would make the row move around between directories, which is worse
    to aim at than a button that is present but inert.
    """
    state = read_state(target)
    if state is None:
        return [], ""

    token = token_for(target)
    dirty = t("file_browser.git_dirty", count=state.dirty) if state.dirty else ""
    line = t(
        "file_browser.git_line",
        branch=state.branch,
        ahead=state.ahead,
        behind=state.known_behind,
        dirty=dirty,
    )

    if not state.has_upstream:
        return [
            InlineKeyboardButton(
                text=t("file_browser.btn_no_upstream"),
                callback_data=f"{SF_PREFIX}{token}",
            )
        ], t("file_browser.git_no_upstream", branch=state.branch)

    # behind is only as fresh as the last fetch, so pull stays available and
    # does the fetching itself; ahead is exact, so push can be inert honestly.
    pull_label = (
        t("file_browser.btn_pull_n", count=state.known_behind)
        if state.known_behind
        else t("file_browser.btn_pull")
    )
    push_label = (
        t("file_browser.btn_push_n", count=state.ahead)
        if state.can_push
        else t("file_browser.btn_push_none")
    )
    return [
        InlineKeyboardButton(text=pull_label, callback_data=f"{SF_PULL_PREFIX}{token}"),
        InlineKeyboardButton(text=push_label, callback_data=f"{SF_PUSH_PREFIX}{token}"),
    ], line


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
