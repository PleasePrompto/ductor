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
    ``sf+<token>``     -- open the upload menu for that directory
    ``sf#<token>``     -- start a file upload into it
    ``sf%<token>``     -- start a folder (.zip) upload into it
    ``sf=<token>``     -- move what is staged into it
    ``sf-<token>``     -- abandon the upload and discard staging
    ``sf/<token>``     -- open the download menu for that directory
    ``sf*<token>``     -- list that directory's files as buttons
    ``sf&<token>``     -- bind this chat/topic to that directory
    ``sf&&<token>``    -- confirm a rebind that would replace an existing one
    ``sf~<token>``     -- manage menu (rename / new folder / delete)
    ``sf;<token>``     -- start a rename, then wait for the name
    ``sf^<token>``     -- start a new folder, then wait for the name
    ``sf_``            -- apply the named rename or folder, once confirmed
    ``sf,<token>``     -- delete, first confirmation
    ``sf|<token>``     -- delete, second confirmation
    ``sf$<token>``     -- delete, carry it out
"""

from __future__ import annotations

import asyncio
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.files.browser import list_directory
from ductor_bot.files.edits import (
    LARGE_DELETE,
    EditStore,
    PendingEdit,
    can_delete,
    can_rename,
    plan_delete,
    sample,
    validate_name,
)
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
from ductor_bot.files.uploads import Mode, UploadSession, UploadStore, plan
from ductor_bot.i18n import t
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

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
SF_UPLOAD_PREFIX = "sf+"
SF_UPLOAD_FILES_PREFIX = "sf#"
SF_UPLOAD_FOLDER_PREFIX = "sf%"
SF_UPLOAD_CONFIRM_PREFIX = "sf="
SF_UPLOAD_CANCEL_PREFIX = "sf-"
SF_DOWNLOAD_PREFIX = "sf/"
SF_FILE_LIST_PREFIX = "sf*"
SF_BIND_PREFIX = "sf&"
#: Rebinding is confirmed: it moves the working directory, so the next
#: command acts somewhere other than where the last one did.
SF_BIND_CONFIRM_PREFIX = "sf&&"
#: Destructive actions live behind one more tap, so they cannot be hit by
#: aiming badly at the row above.
SF_MANAGE_PREFIX = "sf~"
SF_RENAME_PREFIX = "sf;"
SF_NEWDIR_PREFIX = "sf^"
SF_EDIT_APPLY_PREFIX = "sf_"
SF_DELETE_PREFIX = "sf,"
SF_DELETE_AGAIN_PREFIX = "sf|"
SF_DELETE_DO_PREFIX = "sf$"

#: One button per file put a folder's whole contents in the main view. They
#: live behind the download menu now, but a large directory can still overflow
#: what Telegram accepts in one keyboard, so the list is capped and says so.
_MAX_FILE_BUTTONS = 60
#: Same reasoning for the text listing against the 4096-character message cap.
_MAX_LISTED_ENTRIES = 100

_MAX_BUTTONS_PER_ROW = 3
#: Telegram refuses bot uploads past 50 MB; stop before building the archive
#: rather than after spending the time and disk.
_MAX_SEND_BYTES = 45 * 1024 * 1024
_MAX_ZIP_FILES = 2000


@dataclass(frozen=True, slots=True)
class BrowserSession:
    """Per-conversation state the stateful actions need.

    Navigation is a pure function of the path in the callback; only uploads and
    binding care which conversation is asking. Passing one object keeps that
    distinction visible instead of threading three parameters through actions
    that ignore them.
    """

    uploads: UploadStore
    key: str
    current_binding: str | None = None
    edits: EditStore | None = None


@dataclass(frozen=True, slots=True)
class BrowserAction:
    """What the transport should do after a callback."""

    text: str = ""
    keyboard: InlineKeyboardMarkup | None = None
    send_path: Path | None = None
    zip_dir: Path | None = None
    agent_prompt: str | None = None
    #: True when the edited message becomes the one staging is reported into.
    upload_message: bool = False
    #: True when this message becomes the one a typed name is answered into.
    edit_message: bool = False
    #: Set when the user confirmed this chat/topic should work in a directory.
    bind_dir: Path | None = None


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
            SF_UPLOAD_PREFIX,
            SF_UPLOAD_FILES_PREFIX,
            SF_UPLOAD_FOLDER_PREFIX,
            SF_UPLOAD_CONFIRM_PREFIX,
            SF_UPLOAD_CANCEL_PREFIX,
            SF_DOWNLOAD_PREFIX,
            SF_FILE_LIST_PREFIX,
            SF_BIND_PREFIX,
            SF_BIND_CONFIRM_PREFIX,
            SF_MANAGE_PREFIX,
            SF_RENAME_PREFIX,
            SF_NEWDIR_PREFIX,
            SF_DELETE_PREFIX,
            SF_DELETE_AGAIN_PREFIX,
            SF_DELETE_DO_PREFIX,
        )
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def file_browser_start(
    paths: DuctorPaths,
    project_roots: Mapping[str, str],
    start_dir: Path | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the opening ``/showfiles`` view.

    A conversation bound to a folder opens *in* it rather than at the list of
    every root: that folder is where its work happens, and the list is one tap
    away behind Home. Falls back to the root list when *start_dir* is not
    somewhere the browser is allowed to show.
    """
    if start_dir is not None:
        view = await asyncio.to_thread(_start_view, paths, project_roots, start_dir)
        if view is not None:
            return view
    return await asyncio.to_thread(_build_root_view, paths, project_roots)


def _start_view(
    paths: DuctorPaths, project_roots: Mapping[str, str], start_dir: Path
) -> tuple[str, InlineKeyboardMarkup] | None:
    """The directory view for *start_dir*, or None if it cannot be shown."""
    roots = browsable_roots(paths.ductor_home, project_roots)
    if not start_dir.is_dir() or not contains(roots, start_dir):
        return None
    return _build_dir_view(paths, project_roots, start_dir)


async def handle_file_browser_callback(
    paths: DuctorPaths,
    project_roots: Mapping[str, str],
    data: str,
    *,
    session: BrowserSession | None = None,
) -> BrowserAction:
    """Route an ``sf`` callback.

    *session* is only needed by the upload and binding actions, which are the
    stateful ones: everything else is a pure function of the path in the
    callback.
    """
    return await asyncio.to_thread(_handle, paths, project_roots, data, session)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _parse(data: str) -> tuple[str, str] | None:
    """Split callback data into ``(prefix, token)``."""
    # SF_PUSH_CONFIRM_PREFIX starts with SF_FILE_PREFIX, so it must be tried first.
    for prefix in (
        SF_PUSH_CONFIRM_PREFIX,
        SF_BIND_CONFIRM_PREFIX,
        SF_FILE_PREFIX,
        SF_ZIP_PREFIX,
        SF_ASK_PREFIX,
        SF_PULL_PREFIX,
        SF_PUSH_PREFIX,
        SF_UPLOAD_PREFIX,
        SF_UPLOAD_FILES_PREFIX,
        SF_UPLOAD_FOLDER_PREFIX,
        SF_UPLOAD_CONFIRM_PREFIX,
        SF_UPLOAD_CANCEL_PREFIX,
        SF_DOWNLOAD_PREFIX,
        SF_FILE_LIST_PREFIX,
        SF_BIND_PREFIX,
        SF_MANAGE_PREFIX,
        SF_RENAME_PREFIX,
        SF_NEWDIR_PREFIX,
        SF_DELETE_PREFIX,
        SF_DELETE_AGAIN_PREFIX,
        SF_DELETE_DO_PREFIX,
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
    return _with_notice(paths, project_roots, target, _result(t(key), output))


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
    return _with_notice(paths, project_roots, target, _result(t(key), output))


def render_dir_with_notice(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path, notice: str
) -> tuple[str, InlineKeyboardMarkup]:
    """Public form of the directory view plus a result line."""
    action = _with_notice(paths, project_roots, target, notice)
    return action.text, action.keyboard or InlineKeyboardMarkup(inline_keyboard=[])


def _with_notice(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path, notice: str
) -> BrowserAction:
    """Re-render the directory with a result line appended."""
    text, kb = _build_dir_view(paths, project_roots, target)
    return BrowserAction(text=f"{text}\n\n{notice}", keyboard=kb)


def _result(headline: str, output: str) -> str:
    """Headline plus the command output in a fenced block.

    The fence is built here rather than embedded in the translation: a code
    block written inside a TOML string has to survive two layers of escaping,
    and getting that wrong renders as a literal backslash-n in the chat.
    """
    return f"{headline}\n```\n{_trim(output)}\n```"


def _trim(output: str, limit: int = 400) -> str:
    output = output.strip()
    return output if len(output) <= limit else output[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _download_menu_action(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path
) -> BrowserAction:
    """Choose between one file and the whole folder."""
    if not target.is_dir():
        return _root_action(paths, project_roots)
    token = token_for(target)
    rows = [
        [
            InlineKeyboardButton(
                text=t("file_browser.btn_download_file"),
                callback_data=f"{SF_FILE_LIST_PREFIX}{token}",
            ),
            InlineKeyboardButton(
                text=t("file_browser.btn_zip"),
                callback_data=f"{SF_ZIP_PREFIX}{token}",
            ),
        ],
        _nav_row(target),
    ]
    text = fmt(
        t("file_browser.header"),
        SEP,
        t("file_browser.download_menu", dir=_display(paths, project_roots, target)),
    )
    return BrowserAction(text=text, keyboard=InlineKeyboardMarkup(inline_keyboard=rows))


def _file_list_action(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path
) -> BrowserAction:
    """One button per file, so a tap sends it."""
    if not target.is_dir():
        return _root_action(paths, project_roots)
    _dirs, files = list_directory(target)
    shown = files[:_MAX_FILE_BUTTONS]

    rows = _rows(
        [
            InlineKeyboardButton(
                text=f"📄 {f}", callback_data=f"{SF_FILE_PREFIX}{token_for(target / f)}"
            )
            for f in shown
        ]
    )
    rows.append(_nav_row(target))

    body = t("file_browser.pick_file", dir=_display(paths, project_roots, target))
    if not files:
        body = t("file_browser.no_files")
    elif len(files) > len(shown):
        body = f"{body}\n\n{t('file_browser.file_list_truncated', count=len(shown))}"

    text = fmt(t("file_browser.header"), SEP, body)
    return BrowserAction(text=text, keyboard=InlineKeyboardMarkup(inline_keyboard=rows))


def _nav_row(target: Path) -> list[InlineKeyboardButton]:
    """Back to the folder, or straight to the root list."""
    return [
        InlineKeyboardButton(
            text=t("file_browser.btn_back"),
            callback_data=f"{SF_PREFIX}{token_for(target)}",
        ),
        InlineKeyboardButton(text=t("file_browser.btn_home"), callback_data=SF_PREFIX),
    ]


def _bind_action(
    paths: DuctorPaths,
    project_roots: Mapping[str, str],
    target: Path,
    current_binding: str | None,
) -> BrowserAction:
    """Bind this chat to *target*, asking first if it would replace a binding."""
    if not target.is_dir():
        return _root_action(paths, project_roots)
    if current_binding and current_binding != str(target):
        confirm = [
            InlineKeyboardButton(
                text=t("file_browser.btn_bind_confirm"),
                callback_data=f"{SF_BIND_CONFIRM_PREFIX}{token_for(target)}",
            ),
            InlineKeyboardButton(
                text=t("file_browser.btn_cancel"),
                callback_data=f"{SF_PREFIX}{token_for(target)}",
            ),
        ]
        text, _ = _build_dir_view(paths, project_roots, target)
        body = t(
            "file_browser.bind_confirm",
            # Both sides go through the same label: showing a friendly name
            # against a raw absolute path made the two look unrelated.
            current=_display(paths, project_roots, Path(current_binding)),
            new=_display(paths, project_roots, target),
        )
        return BrowserAction(
            text=f"{text}\n\n{body}",
            keyboard=InlineKeyboardMarkup(inline_keyboard=[confirm]),
        )
    return _bind_confirmed_action(paths, project_roots, target)


_BIND_ACTIONS: dict[str, Callable[..., BrowserAction]] = {}


def _bind_confirmed_action(
    paths: DuctorPaths,
    project_roots: Mapping[str, str],
    target: Path,
    _current_binding: str | None = None,
) -> BrowserAction:
    if not target.is_dir():
        return _root_action(paths, project_roots)
    action = _with_notice(
        paths,
        project_roots,
        target,
        t("file_browser.bound", dir=_display(paths, project_roots, target)),
    )
    return replace(action, bind_dir=target)


_BIND_ACTIONS[SF_BIND_PREFIX] = _bind_action
_BIND_ACTIONS[SF_BIND_CONFIRM_PREFIX] = _bind_confirmed_action


#: Prefixes whose actions need a BrowserSession.
_STATEFUL_PREFIXES = frozenset(
    {
        SF_BIND_PREFIX,
        SF_BIND_CONFIRM_PREFIX,
        SF_UPLOAD_PREFIX,
        SF_UPLOAD_FILES_PREFIX,
        SF_UPLOAD_FOLDER_PREFIX,
        SF_UPLOAD_CONFIRM_PREFIX,
        SF_UPLOAD_CANCEL_PREFIX,
    }
)


# ---------------------------------------------------------------------------
# Rename, new folder, delete
# ---------------------------------------------------------------------------


def _human_size(total: int) -> str:
    """A size a person can judge. Rounding 600 bytes up to "1 MB" is a lie."""
    if total >= 1048576:
        return f"{total // 1048576} MB"
    if total >= 1024:
        return f"{total // 1024} KB"
    return f"{total} B"


def _roots_map(paths: DuctorPaths, project_roots: Mapping[str, str]) -> dict[str, Path]:
    """Every directory the user declared, not just the ones the picker shows.

    ``browsable_roots`` collapses nested entries — with ``IT`` configured, the
    twelve projects under it fold into one. Protecting only that set would
    leave a configured root deletable whenever it happens to sit inside
    another, which is the normal arrangement rather than the exception.
    """
    roots = dict(browsable_roots(paths.ductor_home, project_roots))
    for label, raw in project_roots.items():
        path = Path(raw).expanduser()
        if path.is_dir():
            roots.setdefault(f"cfg:{label}", path)
    return roots


def _manage_action(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path, session: BrowserSession
) -> BrowserAction:
    """The menu that changes files, one tap away from the browsing controls."""
    if session.edits is not None:
        session.edits.end(session.key)
    if not target.exists():
        return _root_action(paths, project_roots)

    token = token_for(target)
    rows = [
        [
            InlineKeyboardButton(
                text=t("edits.btn_rename"), callback_data=f"{SF_RENAME_PREFIX}{token}"
            ),
            InlineKeyboardButton(
                text=t("edits.btn_newdir"), callback_data=f"{SF_NEWDIR_PREFIX}{token}"
            ),
        ],
        [
            InlineKeyboardButton(
                text=t("edits.btn_delete"), callback_data=f"{SF_DELETE_PREFIX}{token}"
            )
        ],
        [
            InlineKeyboardButton(
                text=t("file_browser.btn_back"), callback_data=f"{SF_PREFIX}{token}"
            )
        ],
    ]
    body = t("edits.manage", dir=_display(paths, project_roots, target))
    return BrowserAction(
        text=fmt(t("file_browser.header"), SEP, body),
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _ask_name_action(kind: str):  # noqa: ANN202
    """Build the action that starts a rename or a new folder."""

    def action(
        paths: DuctorPaths,
        project_roots: Mapping[str, str],
        target: Path,
        session: BrowserSession,
    ) -> BrowserAction:
        if session.edits is None or not target.exists():
            return _open_action(paths, project_roots, target)
        if kind == "rename":
            refusal = can_rename(target, _roots_map(paths, project_roots))
            if refusal:
                return _with_notice(paths, project_roots, target.parent, t(refusal))

        session.edits.begin(session.key, kind, target)  # type: ignore[arg-type]
        body = t(
            "edits.ask_rename" if kind == "rename" else "edits.ask_newdir",
            name=target.name,
            dir=_display(paths, project_roots, target),
        )
        cancel = [
            InlineKeyboardButton(
                text=t("upload.btn_cancel"),
                callback_data=f"{SF_MANAGE_PREFIX}{token_for(target)}",
            )
        ]
        return BrowserAction(
            text=fmt(t("file_browser.header"), SEP, body),
            keyboard=InlineKeyboardMarkup(inline_keyboard=[cancel]),
            edit_message=True,
        )

    return action


def build_name_confirmation(
    paths: DuctorPaths,
    project_roots: Mapping[str, str],
    edit: PendingEdit,
    name: str
) -> tuple[str, InlineKeyboardMarkup]:
    """Show the typed name back before anything is written."""
    refusal = validate_name(name)
    if refusal:
        retry = t(
            "edits.ask_rename" if edit.kind == "rename" else "edits.ask_newdir",
            name=edit.target.name,
            dir=_display(paths, project_roots, edit.target),
        )
        body = f"{t(refusal)}\n\n{retry}"
        rows = [
            [
                InlineKeyboardButton(
                    text=t("upload.btn_cancel"),
                    callback_data=f"{SF_MANAGE_PREFIX}{token_for(edit.target)}",
                )
            ]
        ]
        return fmt(t("file_browser.header"), SEP, body), InlineKeyboardMarkup(inline_keyboard=rows)

    if edit.kind == "rename":
        body = t(
            "edits.confirm_rename",
            old=edit.target.name,
            new=name.strip(),
            dir=_display(paths, project_roots, edit.target.parent),
        )
    else:
        body = t(
            "edits.confirm_newdir",
            new=name.strip(),
            dir=_display(paths, project_roots, edit.target),
        )
    rows = [
        [
            InlineKeyboardButton(text=t("edits.btn_apply"), callback_data=SF_EDIT_APPLY_PREFIX),
            InlineKeyboardButton(
                text=t("upload.btn_cancel"),
                callback_data=f"{SF_MANAGE_PREFIX}{token_for(edit.target)}",
            ),
        ]
    ]
    return fmt(t("file_browser.header"), SEP, body), InlineKeyboardMarkup(inline_keyboard=rows)


def _delete_step_one(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path, _session: BrowserSession
) -> BrowserAction:
    """Say exactly what would go, and refuse the cases no dialog makes safe."""
    refusal = can_delete(target, _roots_map(paths, project_roots))
    if refusal:
        return _with_notice(paths, project_roots, target.parent, t(refusal))

    detail = plan_delete(target)
    listing = "\n".join(f"  {line}" for line in sample(target))
    body = t(
        "edits.delete_first",
        name=target.name,
        count=detail.files,
        size=_human_size(detail.bytes),
    )
    rows = [
        [
            InlineKeyboardButton(
                text=t("edits.btn_delete_continue"),
                callback_data=f"{SF_DELETE_AGAIN_PREFIX}{token_for(target)}",
            ),
            InlineKeyboardButton(
                text=t("upload.btn_cancel"),
                callback_data=f"{SF_MANAGE_PREFIX}{token_for(target)}",
            ),
        ]
    ]
    return BrowserAction(
        text=fmt(t("file_browser.header"), SEP, f"{body}\n\n{listing}"),
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _delete_step_two(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path, _session: BrowserSession
) -> BrowserAction:
    """The second confirmation. Re-checks the refusals: the tree may have moved."""
    refusal = can_delete(target, _roots_map(paths, project_roots))
    if refusal:
        return _with_notice(paths, project_roots, target.parent, t(refusal))

    detail = plan_delete(target)
    body = t(
        "edits.delete_second",
        name=target.name,
        count=detail.files,
        dir=_display(paths, project_roots, target.parent),
    )
    if detail.files > LARGE_DELETE:
        body = f"{body}\n\n{t('edits.delete_large', count=detail.files)}"
    rows = [
        [
            InlineKeyboardButton(
                text=t("edits.btn_delete_now"),
                callback_data=f"{SF_DELETE_DO_PREFIX}{token_for(target)}",
            ),
            InlineKeyboardButton(
                text=t("upload.btn_cancel"),
                callback_data=f"{SF_MANAGE_PREFIX}{token_for(target)}",
            ),
        ]
    ]
    return BrowserAction(
        text=fmt(t("file_browser.header"), SEP, body),
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _delete_do(
    paths: DuctorPaths, project_roots: Mapping[str, str], target: Path, _session: BrowserSession
) -> BrowserAction:
    """Carry it out. Guards are checked a third time, immediately before."""
    from ductor_bot.files.edits import apply_delete

    refusal = can_delete(target, _roots_map(paths, project_roots))
    if refusal:
        return _with_notice(paths, project_roots, target.parent, t(refusal))

    parent = target.parent
    name = target.name
    try:
        apply_delete(target)
    except OSError:
        return _with_notice(paths, project_roots, parent, t("edits.failed", name=name))
    return _with_notice(paths, project_roots, parent, t("edits.deleted", name=name))


_EDIT_ACTIONS = {
    SF_MANAGE_PREFIX: _manage_action,
    SF_RENAME_PREFIX: _ask_name_action("rename"),
    SF_NEWDIR_PREFIX: _ask_name_action("newdir"),
    SF_DELETE_PREFIX: _delete_step_one,
    SF_DELETE_AGAIN_PREFIX: _delete_step_two,
    SF_DELETE_DO_PREFIX: _delete_do,
}


_ACTIONS = {
    SF_ASK_PREFIX: _ask_action,
    SF_FILE_PREFIX: _send_file_action,
    SF_ZIP_PREFIX: _zip_action,
    SF_PREFIX: _open_action,
    SF_PULL_PREFIX: _pull_action,
    SF_PUSH_PREFIX: _push_action,
    SF_PUSH_CONFIRM_PREFIX: _push_confirmed_action,
    SF_DOWNLOAD_PREFIX: _download_menu_action,
    SF_FILE_LIST_PREFIX: _file_list_action,
}


def _handle(  # noqa: PLR0911
    paths: DuctorPaths,
    project_roots: Mapping[str, str],
    data: str,
    session: BrowserSession | None = None,
) -> BrowserAction:
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

    if prefix in _EDIT_ACTIONS or prefix in _STATEFUL_PREFIXES:
        # Without a session there is nowhere to record the result; fall back to
        # plain navigation rather than opening a mode that cannot work.
        if session is None:
            return _open_action(paths, project_roots, target)
        if prefix in _EDIT_ACTIONS:
            return _EDIT_ACTIONS[prefix](paths, project_roots, target, session)
        if prefix in _BIND_ACTIONS:
            return _BIND_ACTIONS[prefix](paths, project_roots, target, session.current_binding)
        return _UPLOAD_ACTIONS[prefix](paths, project_roots, target, session.uploads, session.key)
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
    if len(lines) > _MAX_LISTED_ENTRIES:
        hidden = len(lines) - _MAX_LISTED_ENTRIES
        lines = [*lines[:_MAX_LISTED_ENTRIES], f"  {t('upload.more', count=hidden)}"]
    if not lines:
        lines.append(f"  {t('file_browser.empty')}")

    text = fmt(
        t("file_browser.header"),
        SEP,
        f"`{display}`\n\n" + "\n".join(lines),
        SEP,
        t("file_browser.tap_hint"),
    )

    # Only directories get a button here: they are navigation. Files are
    # reachable through the download menu, which keeps a folder of any size to
    # a screen you can actually aim at.
    rows = _rows(
        [
            InlineKeyboardButton(text=f"{d}/", callback_data=f"{SF_PREFIX}{token_for(target / d)}")
            for d in dirs
        ]
    )

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
                text=t("file_browser.btn_download"),
                callback_data=f"{SF_DOWNLOAD_PREFIX}{token_for(target)}",
            ),
            InlineKeyboardButton(
                text=t("upload.btn_upload"),
                callback_data=f"{SF_UPLOAD_PREFIX}{token_for(target)}",
            ),
        ]
    )
    # "Ask" gets its own row: three long labels on one row squeeze each other
    # down to a few characters on a phone.
    rows.append(
        [
            InlineKeyboardButton(
                text=t("file_browser.btn_ask"),
                callback_data=f"{SF_ASK_PREFIX}{token_for(target)}",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=t("file_browser.btn_bind"),
                callback_data=f"{SF_BIND_PREFIX}{token_for(target)}",
            ),
            InlineKeyboardButton(
                text=t("edits.btn_manage"),
                callback_data=f"{SF_MANAGE_PREFIX}{token_for(target)}",
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


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

#: Telegram messages die at 4096 characters, and a listing is worth reading
#: only while it fits on a phone screen anyway.
_MAX_LISTED_ITEMS = 20



def _upload_menu_action(
    paths: DuctorPaths,
    project_roots: Mapping[str, str],
    target: Path,
    _uploads: UploadStore,
    _key: str,
) -> BrowserAction:
    """Ask what kind of upload this is, rather than guessing from the file.

    A zip meant to be stored and a zip meant to be unpacked are the same bytes;
    only the sender knows which, so the choice is made before anything arrives.
    """
    if not target.is_dir():
        return _root_action(paths, project_roots)
    rows = [
        [
            InlineKeyboardButton(
                text=t("upload.btn_files"),
                callback_data=f"{SF_UPLOAD_FILES_PREFIX}{token_for(target)}",
            ),
            InlineKeyboardButton(
                text=t("upload.btn_folder"),
                callback_data=f"{SF_UPLOAD_FOLDER_PREFIX}{token_for(target)}",
            ),
        ],
        [
            InlineKeyboardButton(
                text=t("file_browser.btn_back"),
                callback_data=f"{SF_PREFIX}{token_for(target)}",
            )
        ],
    ]
    text = fmt(
        t("file_browser.header"),
        SEP,
        t("upload.menu", dir=_display(paths, project_roots, target)),
    )
    return BrowserAction(text=text, keyboard=InlineKeyboardMarkup(inline_keyboard=rows))


def _upload_starter(
    mode: Mode,
) -> Callable[[DuctorPaths, Mapping[str, str], Path, UploadStore, str], BrowserAction]:
    """Build the action that opens an upload in *mode*."""

    def action(
        paths: DuctorPaths,
        project_roots: Mapping[str, str],
        target: Path,
        uploads: UploadStore,
        key: str,
    ) -> BrowserAction:
        if not target.is_dir():
            return _root_action(paths, project_roots)
        uploads.begin(key, target, mode)
        body = t(
            "upload.await_files" if mode == "files" else "upload.await_folder",
            dir=_display(paths, project_roots, target),
        )
        return BrowserAction(
            text=fmt(t("file_browser.header"), SEP, body),
            keyboard=_staging_keyboard(target, count=0),
            upload_message=True,
        )

    return action


def _upload_confirm_action(
    paths: DuctorPaths,
    project_roots: Mapping[str, str],
    target: Path,
    uploads: UploadStore,
    key: str,
) -> BrowserAction:
    session = uploads.get(key)
    if session is None:
        return _open_action(paths, project_roots, target)
    dest = session.dest
    moved = uploads.commit(key)
    notice = (
        t("upload.moved", count=moved, dir=_display(paths, project_roots, dest))
        if moved
        else t("upload.nothing")
    )
    # Confirming ends the upload, so this lands back on the folder itself.
    return _with_notice(paths, project_roots, dest, notice)


def _upload_cancel_action(
    paths: DuctorPaths,
    project_roots: Mapping[str, str],
    target: Path,
    uploads: UploadStore,
    key: str,
) -> BrowserAction:
    session = uploads.get(key)
    dest = session.dest if session is not None else target
    uploads.end(key)
    return _with_notice(paths, project_roots, dest, t("upload.cancelled"))


def build_staging_view(
    paths: DuctorPaths, project_roots: Mapping[str, str], session: UploadSession
) -> tuple[str, InlineKeyboardMarkup]:
    """The list of what is waiting, edited in place as more arrives."""
    items = plan(session)
    dest = _display(paths, project_roots, session.dest)

    lines = [
        "  {}{}".format(
            item.name,
            f"  {t('upload.overwrite_marker')}" if item.overwrites else "",
        )
        for item in items[:_MAX_LISTED_ITEMS]
    ]
    if len(items) > _MAX_LISTED_ITEMS:
        lines.append(f"  {t('upload.more', count=len(items) - _MAX_LISTED_ITEMS)}")
    if not items:
        lines.append(f"  {t('upload.nothing')}")

    body = [t("upload.staging_header", dir=dest), "", *lines]
    overwrites = sum(1 for item in items if item.overwrites)
    if overwrites:
        body += ["", t("upload.overwrite_note", count=overwrites)]
    if session.errors:
        body += ["", *session.errors]

    text = fmt(t("file_browser.header"), SEP, "\n".join(body))
    return text, _staging_keyboard(session.dest, count=len(items))


def _staging_keyboard(dest: Path, *, count: int) -> InlineKeyboardMarkup:
    token = token_for(dest)
    row = []
    if count:
        row.append(
            InlineKeyboardButton(
                text=t("upload.btn_confirm", count=count),
                callback_data=f"{SF_UPLOAD_CONFIRM_PREFIX}{token}",
            )
        )
    row.append(
        InlineKeyboardButton(
            text=t("upload.btn_cancel"),
            callback_data=f"{SF_UPLOAD_CANCEL_PREFIX}{token}",
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def _display(paths: DuctorPaths, project_roots: Mapping[str, str], target: Path) -> str:
    """``Label/sub/dir`` for a path, falling back to its name."""
    owner = label_for(browsable_roots(paths.ductor_home, project_roots), target)
    if owner is None:
        return target.name
    label, root = owner
    rel = target.relative_to(root)
    return f"{label}/" if str(rel) == "." else f"{label}/{rel}"


_UPLOAD_ACTIONS = {
    SF_UPLOAD_PREFIX: _upload_menu_action,
    SF_UPLOAD_FILES_PREFIX: _upload_starter("files"),
    SF_UPLOAD_FOLDER_PREFIX: _upload_starter("folder"),
    SF_UPLOAD_CONFIRM_PREFIX: _upload_confirm_action,
    SF_UPLOAD_CANCEL_PREFIX: _upload_cancel_action,
}


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
