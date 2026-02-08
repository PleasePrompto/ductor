"""Tests for bot media handling: download, index, prompt injection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import yaml
from aiogram.types import Message


def _make_message(
    *,
    text: str | None = None,
    photo: bool = False,
    voice: bool = False,
    document: bool = False,
    video: bool = False,
    audio: bool = False,
    sticker: bool = False,
    video_note: bool = False,
    caption: str | None = None,
    chat_type: str = "private",
) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.chat = MagicMock()
    msg.chat.id = 1
    msg.chat.type = chat_type
    msg.text = text
    msg.caption = caption
    msg.caption_entities = None
    msg.reply_to_message = None
    msg.answer = MagicMock()

    # Media attributes
    msg.photo = None
    msg.voice = None
    msg.document = None
    msg.video = None
    msg.audio = None
    msg.sticker = None
    msg.video_note = None

    if photo:
        p = MagicMock()
        p.file_unique_id = "abc123"
        msg.photo = [p]  # Last element = highest quality

    if voice:
        v = MagicMock()
        v.file_unique_id = "voice1"
        v.mime_type = "audio/ogg"
        msg.voice = v

    if document:
        d = MagicMock()
        d.file_unique_id = "doc1"
        d.file_name = "report.pdf"
        d.mime_type = "application/pdf"
        msg.document = d

    if video:
        v = MagicMock()
        v.file_unique_id = "vid1"
        v.file_name = None
        v.mime_type = "video/mp4"
        msg.video = v

    if audio:
        a = MagicMock()
        a.file_unique_id = "aud1"
        a.file_name = "song.mp3"
        a.mime_type = "audio/mpeg"
        msg.audio = a

    if sticker:
        s = MagicMock()
        s.file_unique_id = "stk1"
        s.is_animated = False
        s.is_video = False
        msg.sticker = s

    if video_note:
        vn = MagicMock()
        vn.file_unique_id = "vn1"
        msg.video_note = vn

    return msg


# ---------------------------------------------------------------------------
# has_media
# ---------------------------------------------------------------------------


class TestHasMedia:
    def test_text_only(self) -> None:
        from ductor_bot.bot.media import has_media

        assert has_media(_make_message(text="hello")) is False

    def test_photo(self) -> None:
        from ductor_bot.bot.media import has_media

        assert has_media(_make_message(photo=True)) is True

    def test_voice(self) -> None:
        from ductor_bot.bot.media import has_media

        assert has_media(_make_message(voice=True)) is True

    def test_document(self) -> None:
        from ductor_bot.bot.media import has_media

        assert has_media(_make_message(document=True)) is True

    def test_video(self) -> None:
        from ductor_bot.bot.media import has_media

        assert has_media(_make_message(video=True)) is True

    def test_sticker(self) -> None:
        from ductor_bot.bot.media import has_media

        assert has_media(_make_message(sticker=True)) is True

    def test_video_note(self) -> None:
        from ductor_bot.bot.media import has_media

        assert has_media(_make_message(video_note=True)) is True


# ---------------------------------------------------------------------------
# is_media_addressed
# ---------------------------------------------------------------------------


class TestIsMediaAddressed:
    def test_reply_to_bot(self) -> None:
        from ductor_bot.bot.media import is_media_addressed

        msg = _make_message(photo=True, chat_type="group")
        msg.reply_to_message = MagicMock()
        msg.reply_to_message.from_user = MagicMock()
        msg.reply_to_message.from_user.id = 42

        assert is_media_addressed(msg, bot_id=42, bot_username="mybot") is True

    def test_caption_mention(self) -> None:
        from ductor_bot.bot.media import is_media_addressed

        msg = _make_message(photo=True, caption="@mybot look at this", chat_type="group")
        entity = MagicMock()
        entity.type = "mention"
        entity.offset = 0
        entity.length = 6
        msg.caption_entities = [entity]

        assert is_media_addressed(msg, bot_id=42, bot_username="mybot") is True

    def test_not_addressed(self) -> None:
        from ductor_bot.bot.media import is_media_addressed

        msg = _make_message(photo=True, chat_type="group")
        assert is_media_addressed(msg, bot_id=42, bot_username="mybot") is False


# ---------------------------------------------------------------------------
# Media extractors
# ---------------------------------------------------------------------------


class TestResolveMedia:
    def test_photo_extraction(self) -> None:
        from ductor_bot.bot.media import _resolve_media

        msg = _make_message(photo=True)
        kind, obj, name, mime = _resolve_media(msg)

        assert kind == "photo"
        assert obj is not None
        assert name == "photo_abc123.jpg"
        assert mime == "image/jpeg"

    def test_voice_extraction(self) -> None:
        from ductor_bot.bot.media import _resolve_media

        msg = _make_message(voice=True)
        kind, _obj, name, mime = _resolve_media(msg)

        assert kind == "voice"
        assert name == "voice_voice1.ogg"
        assert mime == "audio/ogg"

    def test_document_extraction(self) -> None:
        from ductor_bot.bot.media import _resolve_media

        msg = _make_message(document=True)
        kind, _obj, name, mime = _resolve_media(msg)

        assert kind == "document"
        assert name == "report.pdf"
        assert mime == "application/pdf"

    def test_video_extraction(self) -> None:
        from ductor_bot.bot.media import _resolve_media

        msg = _make_message(video=True)
        kind, _obj, name, mime = _resolve_media(msg)

        assert kind == "video"
        assert name == "video_vid1.mp4"
        assert mime == "video/mp4"

    def test_sticker_static(self) -> None:
        from ductor_bot.bot.media import _resolve_media

        msg = _make_message(sticker=True)
        kind, _, name, mime = _resolve_media(msg)

        assert kind == "sticker"
        assert name == "sticker_stk1.webp"
        assert mime == "image/webp"

    def test_video_note_extraction(self) -> None:
        from ductor_bot.bot.media import _resolve_media

        msg = _make_message(video_note=True)
        kind, _, name, mime = _resolve_media(msg)

        assert kind == "video_note"
        assert name == "videonote_vn1.mp4"
        assert mime == "video/mp4"

    def test_no_media(self) -> None:
        from ductor_bot.bot.media import _resolve_media

        msg = _make_message(text="hello")
        kind, obj, _name, _mime = _resolve_media(msg)

        assert kind is None
        assert obj is None


# ---------------------------------------------------------------------------
# Filename sanitization
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    def test_removes_slashes(self) -> None:
        from ductor_bot.bot.media import _sanitize_filename

        assert _sanitize_filename("path/to/file.txt") == "path_to_file.txt"

    def test_removes_null_bytes(self) -> None:
        from ductor_bot.bot.media import _sanitize_filename

        assert _sanitize_filename("file\x00name.txt") == "filename.txt"

    def test_collapses_underscores(self) -> None:
        from ductor_bot.bot.media import _sanitize_filename

        assert _sanitize_filename("a___b.txt") == "a_b.txt"

    def test_truncates_long_names(self) -> None:
        from ductor_bot.bot.media import _sanitize_filename

        result = _sanitize_filename("x" * 200)
        assert len(result) <= 120

    def test_empty_returns_file(self) -> None:
        from ductor_bot.bot.media import _sanitize_filename

        assert _sanitize_filename("...") == "file"


# ---------------------------------------------------------------------------
# Destination preparation
# ---------------------------------------------------------------------------


class TestPrepareDestination:
    def test_creates_date_dir(self, tmp_path: Path) -> None:
        from ductor_bot.bot.media import _prepare_destination

        dest = _prepare_destination(tmp_path, "test.jpg")
        assert dest.parent.exists()
        # Date directory name format: YYYY-MM-DD
        assert len(dest.parent.name) == 10
        assert dest.parent.name[4] == "-"

    def test_collision_avoidance(self, tmp_path: Path) -> None:
        from ductor_bot.bot.media import _prepare_destination

        dest1 = _prepare_destination(tmp_path, "test.jpg")
        dest1.touch()

        dest2 = _prepare_destination(tmp_path, "test.jpg")
        assert dest2 != dest1
        assert "test_1.jpg" in dest2.name


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class TestUpdateIndex:
    def test_builds_index(self, tmp_path: Path) -> None:
        from ductor_bot.bot.media import update_index

        day_dir = tmp_path / "2025-06-15"
        day_dir.mkdir()
        (day_dir / "photo_abc.jpg").write_bytes(b"\xff\xd8" * 10)
        (day_dir / "voice_xyz.ogg").write_bytes(b"\x00" * 50)

        update_index(tmp_path)

        index_path = tmp_path / "_index.yaml"
        assert index_path.exists()

        data = yaml.safe_load(index_path.read_text())
        assert data["total_files"] == 2
        assert "2025-06-15" in data["tree"]
        files = data["tree"]["2025-06-15"]
        names = {f["name"] for f in files}
        assert "photo_abc.jpg" in names
        assert "voice_xyz.ogg" in names

    def test_skips_non_date_dirs(self, tmp_path: Path) -> None:
        from ductor_bot.bot.media import update_index

        (tmp_path / "random_dir").mkdir()
        (tmp_path / "random_dir" / "file.txt").write_text("x")

        update_index(tmp_path)

        data = yaml.safe_load((tmp_path / "_index.yaml").read_text())
        assert data["total_files"] == 0

    def test_empty_dir(self, tmp_path: Path) -> None:
        from ductor_bot.bot.media import update_index

        update_index(tmp_path)

        data = yaml.safe_load((tmp_path / "_index.yaml").read_text())
        assert data["total_files"] == 0
        assert data["tree"] == {}


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


class TestBuildMediaPrompt:
    def test_basic_prompt(self, tmp_path: Path) -> None:
        from ductor_bot.bot.media import MediaInfo, build_media_prompt

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        file_path = workspace / "telegram_files" / "2025-06-15" / "photo_abc.jpg"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        info = MediaInfo(
            path=file_path,
            media_type="image/jpeg",
            file_name="photo_abc.jpg",
            caption=None,
            original_type="photo",
        )
        prompt = build_media_prompt(info, workspace)

        assert "[INCOMING FILE]" in prompt
        assert "telegram_files/2025-06-15/photo_abc.jpg" in prompt
        assert "image/jpeg" in prompt

    def test_voice_prompt_includes_transcription_hint(self, tmp_path: Path) -> None:
        from ductor_bot.bot.media import MediaInfo, build_media_prompt

        info = MediaInfo(
            path=tmp_path / "voice.ogg",
            media_type="audio/ogg",
            file_name="voice.ogg",
            caption=None,
            original_type="voice",
        )
        prompt = build_media_prompt(info, tmp_path)

        assert "transcribe_audio.py" in prompt

    def test_video_prompt_includes_process_hint(self, tmp_path: Path) -> None:
        from ductor_bot.bot.media import MediaInfo, build_media_prompt

        info = MediaInfo(
            path=tmp_path / "video.mp4",
            media_type="video/mp4",
            file_name="video.mp4",
            caption=None,
            original_type="video",
        )
        prompt = build_media_prompt(info, tmp_path)

        assert "process_video.py" in prompt

    def test_caption_included(self, tmp_path: Path) -> None:
        from ductor_bot.bot.media import MediaInfo, build_media_prompt

        info = MediaInfo(
            path=tmp_path / "photo.jpg",
            media_type="image/jpeg",
            file_name="photo.jpg",
            caption="Look at this!",
            original_type="photo",
        )
        prompt = build_media_prompt(info, tmp_path)

        assert "User message: Look at this!" in prompt

    def test_relative_path(self, tmp_path: Path) -> None:
        from ductor_bot.bot.media import MediaInfo, build_media_prompt

        workspace = tmp_path / "workspace"
        file_path = workspace / "telegram_files" / "photo.jpg"

        info = MediaInfo(
            path=file_path,
            media_type="image/jpeg",
            file_name="photo.jpg",
            caption=None,
            original_type="photo",
        )
        prompt = build_media_prompt(info, workspace)

        # Should be relative, not absolute
        assert str(workspace) not in prompt
        assert "telegram_files/photo.jpg" in prompt
