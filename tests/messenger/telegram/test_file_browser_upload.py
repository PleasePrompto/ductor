"""Upload flow in the file browser.

The property under test throughout: the destination folder is untouched until
the user confirms.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ductor_bot.files import path_tokens
from ductor_bot.files.path_tokens import token_for
from ductor_bot.files.uploads import UploadStore
from ductor_bot.i18n import init
from ductor_bot.messenger.telegram import file_browser as fb

KEY = "tg:-100123"


@pytest.fixture(autouse=True)
def _clean() -> None:
    path_tokens.clear()
    init("en")


@pytest.fixture
def env(tmp_path: Path) -> tuple[SimpleNamespace, dict[str, str], UploadStore, Path]:
    home = tmp_path / ".ductor"
    (home / "workspace").mkdir(parents=True)
    proj = tmp_path / "IT" / "EMR"
    proj.mkdir(parents=True)
    (proj / "README.md").write_text("original")
    paths = SimpleNamespace(ductor_home=home, workspace=home / "workspace")
    return paths, {"EMR": str(proj)}, UploadStore(home / "uploads_staging"), proj


def _buttons(kb) -> list:
    return [b for row in kb.inline_keyboard for b in row]


def _dispatch(env, data: str, *, current_binding: str | None = None):
    paths, roots, uploads, _ = env
    session = fb.BrowserSession(uploads=uploads, key=KEY, current_binding=current_binding)
    return fb._handle(paths, roots, data, session)


def test_directory_view_offers_upload(env) -> None:
    paths, roots, _, proj = env
    _text, kb = fb._build_dir_view(paths, roots, proj)
    assert any("Upload" in b.text for b in _buttons(kb))


def test_menu_offers_files_and_folder(env) -> None:
    _, _, _, proj = env
    action = _dispatch(env, f"{fb.SF_UPLOAD_PREFIX}{token_for(proj)}")
    labels = [b.text for b in _buttons(action.keyboard)]
    assert any("Send files" in label for label in labels)
    assert any(".zip" in label for label in labels)
    assert any("Back" in label for label in labels)


def test_starting_an_upload_opens_a_session(env) -> None:
    _, _, uploads, proj = env
    action = _dispatch(env, f"{fb.SF_UPLOAD_FILES_PREFIX}{token_for(proj)}")

    session = uploads.get(KEY)
    assert session is not None
    assert session.dest == proj
    assert session.mode == "files"
    # The transport needs to know this message is the one staging reports into.
    assert action.upload_message is True


def test_folder_mode_is_a_separate_session_mode(env) -> None:
    _, _, uploads, proj = env
    _dispatch(env, f"{fb.SF_UPLOAD_FOLDER_PREFIX}{token_for(proj)}")
    session = uploads.get(KEY)
    assert session is not None
    assert session.mode == "folder"


def test_confirm_moves_staged_files_and_returns_to_the_folder(env) -> None:
    _, _, uploads, proj = env
    _dispatch(env, f"{fb.SF_UPLOAD_FILES_PREFIX}{token_for(proj)}")
    session = uploads.get(KEY)
    assert session is not None
    (session.staging / "new.txt").write_text("data")

    action = _dispatch(env, f"{fb.SF_UPLOAD_CONFIRM_PREFIX}{token_for(proj)}")

    assert (proj / "new.txt").read_text() == "data"
    assert uploads.get(KEY) is None
    # Back on the directory listing, not left in upload mode.
    assert "new.txt" in action.text
    assert any("Download" in b.text for b in _buttons(action.keyboard))


def test_cancel_discards_without_writing(env) -> None:
    _, _, uploads, proj = env
    _dispatch(env, f"{fb.SF_UPLOAD_FILES_PREFIX}{token_for(proj)}")
    session = uploads.get(KEY)
    assert session is not None
    (session.staging / "unwanted.txt").write_text("no")

    action = _dispatch(env, f"{fb.SF_UPLOAD_CANCEL_PREFIX}{token_for(proj)}")

    assert not (proj / "unwanted.txt").exists()
    assert uploads.get(KEY) is None
    assert "cancelled" in action.text.lower()


def test_staging_view_warns_about_overwrites(env) -> None:
    paths, roots, uploads, proj = env
    session = uploads.begin(KEY, proj, "files")
    (session.staging / "README.md").write_text("replacement")
    (session.staging / "fresh.txt").write_text("new")

    text, kb = fb.build_staging_view(paths, roots, session)

    assert "README.md" in text
    assert "replaces" in text
    assert "1 existing file(s) will be replaced" in text
    assert any("Move 2" in b.text for b in _buttons(kb))
    # Still nothing written.
    assert (proj / "README.md").read_text() == "original"


def test_staging_view_has_no_confirm_button_when_empty(env) -> None:
    paths, roots, uploads, proj = env
    session = uploads.begin(KEY, proj, "files")
    _text, kb = fb.build_staging_view(paths, roots, session)
    labels = [b.text for b in _buttons(kb)]
    assert not any("Move" in label for label in labels)
    assert any("Cancel" in label for label in labels)


def test_staging_view_truncates_a_long_listing(env) -> None:
    paths, roots, uploads, proj = env
    session = uploads.begin(KEY, proj, "folder")
    for i in range(fb._MAX_LISTED_ITEMS + 5):
        (session.staging / f"f{i:03d}.txt").write_text(".")

    text, _kb = fb.build_staging_view(paths, roots, session)
    assert "and 5 more" in text


def test_upload_callbacks_are_recognised(env) -> None:
    _, _, _, proj = env
    token = token_for(proj)
    for prefix in (
        fb.SF_UPLOAD_PREFIX,
        fb.SF_UPLOAD_FILES_PREFIX,
        fb.SF_UPLOAD_FOLDER_PREFIX,
        fb.SF_UPLOAD_CONFIRM_PREFIX,
        fb.SF_UPLOAD_CANCEL_PREFIX,
    ):
        assert fb.is_file_browser_callback(f"{prefix}{token}")


def test_upload_prefixes_do_not_collide_with_navigation(env) -> None:
    """``sf!!`` already shadows ``sf!``; a new prefix must not repeat that."""
    _, _, _, proj = env
    token = token_for(proj)
    for prefix in (
        fb.SF_UPLOAD_PREFIX,
        fb.SF_UPLOAD_FILES_PREFIX,
        fb.SF_UPLOAD_FOLDER_PREFIX,
        fb.SF_UPLOAD_CONFIRM_PREFIX,
        fb.SF_UPLOAD_CANCEL_PREFIX,
    ):
        parsed = fb._parse(f"{prefix}{token}")
        assert parsed == (prefix, token)


def test_without_a_session_upload_degrades_to_navigation(env) -> None:
    """No store means no upload; showing the folder beats a dead button."""
    paths, roots, _, proj = env
    action = fb._handle(paths, roots, f"{fb.SF_UPLOAD_FILES_PREFIX}{token_for(proj)}")
    assert action.upload_message is False
    assert "README.md" in action.text


def test_extracted_archive_stages_its_tree(env) -> None:
    """End to end for folder mode, minus Telegram."""
    from ductor_bot.files.archive import extract_archive

    paths, roots, uploads, proj = env
    session = uploads.begin(KEY, proj, "folder")

    src = proj.parent / "bundle.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("docs/a.md", b"one")
        zf.writestr("docs/b.md", b"two")
    extract_archive(src, session.staging)

    text, _kb = fb.build_staging_view(paths, roots, session)
    assert "docs/a.md" in text

    assert uploads.commit(KEY) == 2
    assert (proj / "docs" / "b.md").read_text() == "two"


# ---------------------------------------------------------------------------
# Download menu
# ---------------------------------------------------------------------------


def test_folder_view_has_no_button_per_file(env) -> None:
    """Files live behind the download menu; a folder of any size stays aimable."""
    paths, roots, _, proj = env
    _text, kb = fb._build_dir_view(paths, roots, proj)
    labels = [b.text for b in _buttons(kb)]
    assert not any(label.startswith("📄") for label in labels)
    assert any("Download" in label for label in labels)
    # The name is still listed as text, so the folder's contents are visible.
    assert "README.md" in _text


def test_download_menu_offers_one_file_or_the_zip(env) -> None:
    _, _, _, proj = env
    action = _dispatch(env, f"{fb.SF_DOWNLOAD_PREFIX}{token_for(proj)}")
    labels = [b.text for b in _buttons(action.keyboard)]
    assert any("single file" in label for label in labels)
    assert any("zip" in label for label in labels)
    assert any("Back" in label for label in labels)
    assert any("Home" in label for label in labels)


def test_file_list_gives_each_file_a_button(env) -> None:
    _, _, _, proj = env
    (proj / "notes.md").write_text("x")
    action = _dispatch(env, f"{fb.SF_FILE_LIST_PREFIX}{token_for(proj)}")
    labels = [b.text for b in _buttons(action.keyboard)]
    assert "📄 README.md" in labels
    assert "📄 notes.md" in labels


def test_tapping_a_listed_file_still_sends_it(env) -> None:
    """The file list reuses the existing send callback."""
    _, _, _, proj = env
    action = _dispatch(env, f"{fb.SF_FILE_LIST_PREFIX}{token_for(proj)}")
    send = next(b for b in _buttons(action.keyboard) if b.text == "📄 README.md")
    result = _dispatch(env, send.callback_data)
    assert result.send_path == proj / "README.md"


def test_file_list_is_capped_and_says_so(env) -> None:
    """Telegram rejects an oversized keyboard outright, so this cannot silently grow."""
    _, _, _, proj = env
    for i in range(fb._MAX_FILE_BUTTONS + 10):
        (proj / f"f{i:03d}.txt").write_text(".")

    action = _dispatch(env, f"{fb.SF_FILE_LIST_PREFIX}{token_for(proj)}")
    file_buttons = [b for b in _buttons(action.keyboard) if b.text.startswith("📄")]
    assert len(file_buttons) == fb._MAX_FILE_BUTTONS
    assert f"first {fb._MAX_FILE_BUTTONS} files" in action.text


def test_empty_folder_says_there_are_no_files(env) -> None:
    _, _, _, proj = env
    (proj / "README.md").unlink()
    action = _dispatch(env, f"{fb.SF_FILE_LIST_PREFIX}{token_for(proj)}")
    assert "no files" in action.text.lower()
    assert not any(b.text.startswith("📄") for b in _buttons(action.keyboard))


def test_long_listing_is_truncated(env) -> None:
    """The text listing has its own ceiling against the 4096-character limit."""
    paths, roots, _, proj = env
    for i in range(fb._MAX_LISTED_ENTRIES + 7):
        (proj / f"g{i:03d}.txt").write_text(".")
    text, _kb = fb._build_dir_view(paths, roots, proj)
    assert "and 8 more" in text


def test_download_callbacks_are_recognised(env) -> None:
    _, _, _, proj = env
    token = token_for(proj)
    for prefix in (fb.SF_DOWNLOAD_PREFIX, fb.SF_FILE_LIST_PREFIX):
        assert fb.is_file_browser_callback(f"{prefix}{token}")
        assert fb._parse(f"{prefix}{token}") == (prefix, token)


# ---------------------------------------------------------------------------
# Opening view
# ---------------------------------------------------------------------------


async def test_showfiles_opens_at_the_bound_folder(env) -> None:
    """A bound conversation opens in its folder, not at the list of every root."""
    paths, roots, _, proj = env
    text, kb = await fb.file_browser_start(paths, roots, proj)
    assert "README.md" in text
    assert any("Home" in b.text for b in _buttons(kb)), "the root list stays one tap away"


async def test_showfiles_falls_back_to_the_root_list(env) -> None:
    paths, roots, _, _proj = env
    text, _kb = await fb.file_browser_start(paths, roots, None)
    assert "EMR/" in text


async def test_a_binding_outside_the_roots_is_ignored(env, tmp_path: Path) -> None:
    """A stale or hostile binding must not open a directory the browser hides."""
    paths, roots, _, _proj = env
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    text, _kb = await fb.file_browser_start(paths, roots, outside)
    assert "EMR/" in text, "falls back to the root list"


async def test_a_deleted_binding_falls_back(env) -> None:
    paths, roots, _, proj = env
    text, _kb = await fb.file_browser_start(paths, roots, proj / "gone")
    assert "EMR/" in text
