"""Buttons that share a row must fit on a phone.

Telegram divides a row's width evenly and truncates with no ellipsis, so a long
label beside a short one simply loses its end: "📌 Use this folder for t". This
is invisible on a desktop client and in every other test — it was reported from
a phone screenshot.

The limit is empirical rather than specified. Telegram publishes no width, and
it varies with device and font size; 22 characters is where truncation was
observed on a narrow screen.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ductor_bot.files.edits import EditStore
from ductor_bot.files.uploads import UploadStore
from ductor_bot.i18n import init
from ductor_bot.messenger.telegram import file_browser as fb

LOCALES = ("de", "en", "es", "fr", "id", "nl", "pt", "ru")

#: Labels sharing a row get half the width each.
MAX_PAIRED = 22


def _screens():
    tmp = Path(tempfile.mkdtemp()).resolve()
    home = tmp / ".ductor"
    (home / "workspace").mkdir(parents=True)
    project = tmp / "IT" / "EMR"
    (project / "sub").mkdir(parents=True)
    (project / "f.txt").write_text("x")

    paths = SimpleNamespace(ductor_home=home, workspace=home / "workspace")
    roots = {"EMR": str(project)}
    session = fb.BrowserSession(uploads=UploadStore(home / "u"), key="k", edits=EditStore())

    def run(prefix, target):
        return fb._handle(paths, roots, f"{prefix}{fb.token_for(target)}", session)

    return {
        "folder view": fb._build_dir_view(paths, roots, project)[1],
        "download menu": run(fb.SF_DOWNLOAD_PREFIX, project).keyboard,
        "upload menu": run(fb.SF_UPLOAD_PREFIX, project).keyboard,
        "manage menu": run(fb.SF_MANAGE_PREFIX, project / "sub").keyboard,
        "delete confirm 1": run(fb.SF_DELETE_PREFIX, project / "sub").keyboard,
        "delete confirm 2": run(fb.SF_DELETE_AGAIN_PREFIX, project / "sub").keyboard,
    }


@pytest.mark.parametrize("locale", LOCALES)
def test_paired_labels_fit_on_a_phone(locale: str) -> None:
    init(locale)
    try:
        offenders = [
            f"{screen}: {button.text!r} ({len(button.text)})"
            for screen, keyboard in _screens().items()
            if keyboard
            for row in keyboard.inline_keyboard
            if len(row) > 1
            for button in row
            if len(button.text) > MAX_PAIRED
        ]
        assert not offenders, (
            f"{locale}: labels sharing a row will be truncated:\n  " + "\n  ".join(offenders)
        )
    finally:
        init("en")
