"""Tests for send_rich and send_file utilities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest


class TestSendRich:
    """Test rich text sending with HTML conversion and file extraction."""

    async def test_plain_text_sent_as_html(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_rich(bot, 1, "Hello world")
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 1
        assert "Hello world" in call_kwargs["text"]

    async def test_returns_true_on_full_delivery(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        assert await send_rich(bot, 1, "Hello world") is True

    async def test_returns_false_on_network_error(self) -> None:
        """#160: a swallowed TelegramNetworkError must be reported to the caller."""
        from aiogram.exceptions import TelegramNetworkError

        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock(
            side_effect=TelegramNetworkError(method=MagicMock(), message="boom")
        )
        assert await send_rich(bot, 1, "Hello world") is False

    async def test_file_tags_extracted_and_sent(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()

        text = f"Here is a file <file:{test_file}>"
        await send_rich(bot, 1, text, SendRichOpts(allowed_roots=[tmp_path]))
        bot.send_message.assert_called_once()
        bot.send_document.assert_called_once()

    async def test_reply_to_first_chunk(self) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        reply_msg = MagicMock()
        reply_msg.message_id = 42

        await send_rich(
            bot, 1, "reply text", SendRichOpts(reply_to_message_id=reply_msg.message_id)
        )
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs["reply_parameters"].message_id == 42

    async def test_empty_text_with_file_still_sends_file(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        test_file = tmp_path / "data.csv"
        test_file.write_text("a,b,c")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()

        await send_rich(bot, 1, f"<file:{test_file}>", SendRichOpts(allowed_roots=[tmp_path]))
        bot.send_document.assert_called_once()

    async def test_html_fallback_on_bad_request(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        # First call fails with TelegramBadRequest, second succeeds (plain text)
        from aiogram.exceptions import TelegramBadRequest

        bot.send_message = AsyncMock(
            side_effect=[TelegramBadRequest(MagicMock(), "bad HTML"), None],
        )

        await send_rich(bot, 1, "test")
        assert bot.send_message.call_count == 2


class TestSendRichButtons:
    """Test button keyboard integration in send_rich."""

    async def test_send_rich_with_buttons_attaches_keyboard(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_reply_markup = AsyncMock()

        await send_rich(bot, 1, "Pick:\n\n[button:Yes] [button:No]")
        bot.edit_message_reply_markup.assert_called_once()
        markup = bot.edit_message_reply_markup.call_args.kwargs["reply_markup"]
        assert len(markup.inline_keyboard) == 1
        assert markup.inline_keyboard[0][0].text == "Yes"
        assert markup.inline_keyboard[0][1].text == "No"

    async def test_send_rich_without_buttons_no_keyboard(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.edit_message_reply_markup = AsyncMock()

        await send_rich(bot, 1, "No buttons")
        bot.edit_message_reply_markup.assert_not_called()

    async def test_send_rich_buttons_stripped_from_displayed_text(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 10
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_reply_markup = AsyncMock()

        await send_rich(bot, 1, "Hello\n\n[button:Go]")
        call_text = bot.send_message.call_args.kwargs["text"]
        assert "[button:" not in call_text
        assert "Hello" in call_text

    async def test_send_rich_buttons_with_reply_to(self) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 77
        bot.send_message = AsyncMock(return_value=sent_msg)
        reply_msg = MagicMock()
        reply_msg.message_id = 99
        bot.edit_message_reply_markup = AsyncMock()

        await send_rich(
            bot, 1, "X\n[button:Ok]", SendRichOpts(reply_to_message_id=reply_msg.message_id)
        )
        bot.edit_message_reply_markup.assert_called_once()
        assert bot.edit_message_reply_markup.call_args.kwargs["message_id"] == 77


class TestSendFile:
    """Test individual file sending."""

    async def test_image_sent_as_photo(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")  # JPEG magic bytes

        bot = MagicMock()
        bot.send_photo = AsyncMock()
        await send_file(bot, chat_id=1, path=img)
        bot.send_photo.assert_called_once()

    async def test_non_image_sent_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4")

        bot = MagicMock()
        bot.send_document = AsyncMock()
        await send_file(bot, chat_id=1, path=doc)
        bot.send_document.assert_called_once()

    async def test_unsupported_image_sent_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        img = tmp_path / "photo.heic"
        img.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_photo = AsyncMock()
        bot.send_document = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="image/heic"):
            await send_file(bot, chat_id=1, path=img)

        bot.send_photo.assert_not_called()
        bot.send_document.assert_called_once()

    async def test_supported_video_sent_as_video(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_video = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="video/mp4"):
            await send_file(bot, chat_id=1, path=video)

        bot.send_video.assert_called_once()

    async def test_unsupported_video_sent_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        video = tmp_path / "clip.webm"
        video.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_video = AsyncMock()
        bot.send_document = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="video/webm"):
            await send_file(bot, chat_id=1, path=video)

        bot.send_video.assert_not_called()
        bot.send_document.assert_called_once()

    async def test_supported_audio_sent_as_audio(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        audio = tmp_path / "sound.mp3"
        audio.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_audio = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="audio/mpeg"):
            await send_file(bot, chat_id=1, path=audio)

        bot.send_audio.assert_called_once()

    async def test_unsupported_audio_sent_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        audio = tmp_path / "sound.wav"
        audio.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_audio = AsyncMock()
        bot.send_document = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="audio/wav"):
            await send_file(bot, chat_id=1, path=audio)

        bot.send_audio.assert_not_called()
        bot.send_document.assert_called_once()

    async def test_photo_rejected_retries_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")

        bot = MagicMock()
        bot.send_photo = AsyncMock(side_effect=TelegramBadRequest(MagicMock(), "bad photo"))
        bot.send_document = AsyncMock()
        await send_file(bot, chat_id=1, path=img)
        bot.send_photo.assert_called_once()
        bot.send_document.assert_called_once()

    async def test_video_rejected_retries_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_video = AsyncMock(side_effect=TelegramBadRequest(MagicMock(), "bad video"))
        bot.send_document = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="video/mp4"):
            await send_file(bot, chat_id=1, path=video)

        bot.send_video.assert_called_once()
        bot.send_document.assert_called_once()

    async def test_audio_rejected_retries_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        audio = tmp_path / "sound.mp3"
        audio.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_audio = AsyncMock(side_effect=TelegramBadRequest(MagicMock(), "bad audio"))
        bot.send_document = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="audio/mpeg"):
            await send_file(bot, chat_id=1, path=audio)

        bot.send_audio.assert_called_once()
        bot.send_document.assert_called_once()

    async def test_missing_file_sends_error(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_file(bot, chat_id=1, path=tmp_path / "missing.txt")
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args.kwargs["text"]
        assert "not found" in text.lower()
        assert str(tmp_path / "missing.txt") in text

    async def test_blocked_path_sends_warning(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        f = tmp_path / "secret.txt"
        f.write_text("secret")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        # allowed_roots is empty list = nothing allowed
        await send_file(bot, chat_id=1, path=f, allowed_roots=[Path("/nonexistent")])
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args.kwargs["text"].lower()
        assert "outside" in text
        assert "file_access" in text


class TestExtractFilePaths:
    """Test file path extraction from text."""

    def test_single_file(self) -> None:
        from ductor_bot.messenger.telegram.sender import extract_file_paths

        assert extract_file_paths("see <file:/tmp/a.txt>") == ["/tmp/a.txt"]

    def test_multiple_files(self) -> None:
        from ductor_bot.messenger.telegram.sender import extract_file_paths

        result = extract_file_paths("<file:/a> and <file:/b>")
        assert result == ["/a", "/b"]

    def test_no_files(self) -> None:
        from ductor_bot.messenger.telegram.sender import extract_file_paths

        assert extract_file_paths("no files here") == []


class TestSendFilesFromText:
    """Test post-streaming file extraction and delivery."""

    async def test_sends_files_from_tags(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_files_from_text

        f1 = tmp_path / "a.pdf"
        f1.write_bytes(b"%PDF")
        f2 = tmp_path / "b.csv"
        f2.write_text("x,y")

        bot = MagicMock()
        bot.send_document = AsyncMock()

        text = f"Here are files <file:{f1}> and <file:{f2}>"
        await send_files_from_text(bot, chat_id=1, text=text)
        assert bot.send_document.call_count == 2

    async def test_no_tags_does_nothing(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_files_from_text

        bot = MagicMock()
        bot.send_document = AsyncMock()
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()

        await send_files_from_text(bot, chat_id=1, text="No files here")
        bot.send_document.assert_not_called()
        bot.send_photo.assert_not_called()

    async def test_image_sent_as_photo(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_files_from_text

        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG")

        bot = MagicMock()
        bot.send_photo = AsyncMock()

        await send_files_from_text(bot, chat_id=1, text=f"<file:{img}>")

    async def test_windows_slash_drive_tag_normalized_before_send(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_files_from_text

        bot = MagicMock()

        with (
            patch("ductor_bot.files.tags.is_windows", return_value=True),
            patch(
                "ductor_bot.messenger.telegram.sender.send_file", new_callable=AsyncMock
            ) as mock_send_file,
        ):
            await send_files_from_text(
                bot, chat_id=1, text="<file:/C/Users/alice/output_to_user/out.zip>"
            )

        sent_path = mock_send_file.call_args.args[2]
        assert str(sent_path).replace("\\", "/") == "C:/Users/alice/output_to_user/out.zip"


class TestWindowsTagNormalizationInSendRich:
    async def test_send_rich_normalizes_windows_file_tag(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()

        with (
            patch("ductor_bot.files.tags.is_windows", return_value=True),
            patch(
                "ductor_bot.messenger.telegram.sender.send_file", new_callable=AsyncMock
            ) as mock_send_file,
        ):
            await send_rich(bot, 1, "<file:/C/Users/alice/result.apk>")

        sent_path = mock_send_file.call_args.args[2]
        assert str(sent_path).replace("\\", "/") == "C:/Users/alice/result.apk"


class TestForumTopicSupport:
    """Test message_thread_id propagation through sender functions."""

    async def test_send_rich_passes_thread_id(self) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_rich(bot, 1, "Hello", SendRichOpts(thread_id=77))
        assert bot.send_message.call_args.kwargs["message_thread_id"] == 77

    async def test_send_rich_thread_id_none_by_default(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_rich(bot, 1, "Hello")
        assert bot.send_message.call_args.kwargs.get("message_thread_id") is None

    async def test_send_rich_passes_thread_id_to_files(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        doc = tmp_path / "data.csv"
        doc.write_text("a,b")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()
        await send_rich(bot, 1, f"Here <file:{doc}>", SendRichOpts(thread_id=55))
        assert bot.send_document.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_passes_thread_id_to_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        doc = tmp_path / "test.pdf"
        doc.write_bytes(b"%PDF")

        bot = MagicMock()
        bot.send_document = AsyncMock()
        await send_file(bot, chat_id=1, path=doc, thread_id=55)
        assert bot.send_document.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_passes_thread_id_to_photo(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")

        bot = MagicMock()
        bot.send_photo = AsyncMock()
        await send_file(bot, chat_id=1, path=img, thread_id=55)
        assert bot.send_photo.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_passes_thread_id_to_video(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_video = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="video/mp4"):
            await send_file(bot, chat_id=1, path=video, thread_id=55)

        assert bot.send_video.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_passes_thread_id_to_audio(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        audio = tmp_path / "sound.mp3"
        audio.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_audio = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="audio/mpeg"):
            await send_file(bot, chat_id=1, path=audio, thread_id=55)

        assert bot.send_audio.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_error_message_passes_thread_id(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_file(bot, chat_id=1, path=Path("/nonexistent.txt"), thread_id=33)
        assert bot.send_message.call_args.kwargs["message_thread_id"] == 33

    async def test_send_file_blocked_path_passes_thread_id(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        f = tmp_path / "secret.txt"
        f.write_text("secret")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_file(bot, chat_id=1, path=f, allowed_roots=[Path("/nowhere")], thread_id=33)
        assert bot.send_message.call_args.kwargs["message_thread_id"] == 33

    async def test_send_files_from_text_passes_thread_id(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_files_from_text

        f = tmp_path / "data.csv"
        f.write_text("a,b")

        bot = MagicMock()
        bot.send_document = AsyncMock()
        await send_files_from_text(bot, chat_id=1, text=f"<file:{f}>", thread_id=44)
        assert bot.send_document.call_args.kwargs["message_thread_id"] == 44

    async def test_html_fallback_preserves_thread_id(self) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock(
            side_effect=[TelegramBadRequest(MagicMock(), "bad HTML"), None],
        )
        await send_rich(bot, 1, "test", SendRichOpts(thread_id=88))
        for call in bot.send_message.call_args_list:
            assert call.kwargs["message_thread_id"] == 88
