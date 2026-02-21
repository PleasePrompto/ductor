"""Tests for send_rich and send_file utilities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramBadRequest


class TestSendRich:
    """Test rich text sending with HTML conversion and file extraction."""

    async def test_plain_text_sent_as_html(self) -> None:
        from ductor_bot.bot.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_rich(bot, chat_id=1, text="Hello world")
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 1
        assert "Hello world" in call_kwargs["text"]

    async def test_file_tags_extracted_and_sent(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_rich

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()

        text = f"Here is a file <file:{test_file}>"
        await send_rich(bot, chat_id=1, text=text, allowed_roots=[tmp_path])
        bot.send_message.assert_called_once()
        bot.send_document.assert_called_once()

    async def test_reply_to_first_chunk(self) -> None:
        from ductor_bot.bot.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        reply_msg = MagicMock()
        reply_msg.answer = AsyncMock()

        await send_rich(bot, chat_id=1, text="reply text", reply_to=reply_msg)
        reply_msg.answer.assert_called_once()

    async def test_empty_text_with_file_still_sends_file(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_rich

        test_file = tmp_path / "data.csv"
        test_file.write_text("a,b,c")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()

        await send_rich(bot, chat_id=1, text=f"<file:{test_file}>", allowed_roots=[tmp_path])
        bot.send_document.assert_called_once()

    async def test_html_fallback_on_bad_request(self) -> None:
        from ductor_bot.bot.sender import send_rich

        bot = MagicMock()
        # First call fails with TelegramBadRequest, second succeeds (plain text)
        from aiogram.exceptions import TelegramBadRequest

        bot.send_message = AsyncMock(
            side_effect=[TelegramBadRequest(MagicMock(), "bad HTML"), None],
        )

        await send_rich(bot, chat_id=1, text="test")
        assert bot.send_message.call_count == 2


class TestSendRichButtons:
    """Test button keyboard integration in send_rich."""

    async def test_send_rich_with_buttons_attaches_keyboard(self) -> None:
        from ductor_bot.bot.sender import send_rich

        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_reply_markup = AsyncMock()

        await send_rich(bot, chat_id=1, text="Pick:\n\n[button:Yes] [button:No]")
        bot.edit_message_reply_markup.assert_called_once()
        markup = bot.edit_message_reply_markup.call_args.kwargs["reply_markup"]
        assert len(markup.inline_keyboard) == 1
        assert markup.inline_keyboard[0][0].text == "Yes"
        assert markup.inline_keyboard[0][1].text == "No"

    async def test_send_rich_without_buttons_no_keyboard(self) -> None:
        from ductor_bot.bot.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.edit_message_reply_markup = AsyncMock()

        await send_rich(bot, chat_id=1, text="No buttons")
        bot.edit_message_reply_markup.assert_not_called()

    async def test_send_rich_buttons_stripped_from_displayed_text(self) -> None:
        from ductor_bot.bot.sender import send_rich

        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 10
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_reply_markup = AsyncMock()

        await send_rich(bot, chat_id=1, text="Hello\n\n[button:Go]")
        call_text = bot.send_message.call_args.kwargs["text"]
        assert "[button:" not in call_text
        assert "Hello" in call_text

    async def test_send_rich_buttons_with_reply_to(self) -> None:
        from ductor_bot.bot.sender import send_rich

        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 77
        reply_msg = MagicMock()
        reply_msg.answer = AsyncMock(return_value=sent_msg)
        bot.edit_message_reply_markup = AsyncMock()

        await send_rich(bot, chat_id=1, text="X\n[button:Ok]", reply_to=reply_msg)
        bot.edit_message_reply_markup.assert_called_once()
        assert bot.edit_message_reply_markup.call_args.kwargs["message_id"] == 77


class TestSendFile:
    """Test individual file sending."""

    async def test_image_sent_as_photo(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_file

        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")  # JPEG magic bytes

        bot = MagicMock()
        bot.send_photo = AsyncMock()
        await send_file(bot, chat_id=1, path=img)
        bot.send_photo.assert_called_once()

    async def test_non_image_sent_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_file

        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4")

        bot = MagicMock()
        bot.send_document = AsyncMock()
        await send_file(bot, chat_id=1, path=doc)
        bot.send_document.assert_called_once()

    async def test_missing_file_sends_error(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_file

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_file(bot, chat_id=1, path=tmp_path / "missing.txt")
        bot.send_message.assert_called_once()
        assert "not found" in bot.send_message.call_args.kwargs["text"].lower()

    async def test_blocked_path_sends_warning(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_file

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
        from ductor_bot.bot.sender import extract_file_paths

        assert extract_file_paths("see <file:/tmp/a.txt>") == ["/tmp/a.txt"]

    def test_multiple_files(self) -> None:
        from ductor_bot.bot.sender import extract_file_paths

        result = extract_file_paths("<file:/a> and <file:/b>")
        assert result == ["/a", "/b"]

    def test_no_files(self) -> None:
        from ductor_bot.bot.sender import extract_file_paths

        assert extract_file_paths("no files here") == []


class TestSendFilesFromText:
    """Test post-streaming file extraction and delivery."""

    async def test_sends_files_from_tags(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_files_from_text

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
        from ductor_bot.bot.sender import send_files_from_text

        bot = MagicMock()
        bot.send_document = AsyncMock()
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()

        await send_files_from_text(bot, chat_id=1, text="No files here")
        bot.send_document.assert_not_called()
        bot.send_photo.assert_not_called()

    async def test_image_sent_as_photo(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_files_from_text

        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG")

        bot = MagicMock()
        bot.send_photo = AsyncMock()

        await send_files_from_text(bot, chat_id=1, text=f"<file:{img}>")


class TestForumTopicSupport:
    """Test message_thread_id propagation through sender functions."""

    async def test_send_rich_passes_thread_id(self) -> None:
        from ductor_bot.bot.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_rich(bot, chat_id=1, text="Hello", thread_id=77)
        assert bot.send_message.call_args.kwargs["message_thread_id"] == 77

    async def test_send_rich_thread_id_none_by_default(self) -> None:
        from ductor_bot.bot.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_rich(bot, chat_id=1, text="Hello")
        assert bot.send_message.call_args.kwargs.get("message_thread_id") is None

    async def test_send_rich_passes_thread_id_to_files(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_rich

        doc = tmp_path / "data.csv"
        doc.write_text("a,b")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()
        await send_rich(bot, chat_id=1, text=f"Here <file:{doc}>", thread_id=55)
        assert bot.send_document.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_passes_thread_id_to_document(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_file

        doc = tmp_path / "test.pdf"
        doc.write_bytes(b"%PDF")

        bot = MagicMock()
        bot.send_document = AsyncMock()
        await send_file(bot, chat_id=1, path=doc, thread_id=55)
        assert bot.send_document.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_passes_thread_id_to_photo(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_file

        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")

        bot = MagicMock()
        bot.send_photo = AsyncMock()
        await send_file(bot, chat_id=1, path=img, thread_id=55)
        assert bot.send_photo.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_error_message_passes_thread_id(self) -> None:
        from ductor_bot.bot.sender import send_file

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_file(bot, chat_id=1, path=Path("/nonexistent.txt"), thread_id=33)
        assert bot.send_message.call_args.kwargs["message_thread_id"] == 33

    async def test_send_file_blocked_path_passes_thread_id(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_file

        f = tmp_path / "secret.txt"
        f.write_text("secret")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_file(bot, chat_id=1, path=f, allowed_roots=[Path("/nowhere")], thread_id=33)
        assert bot.send_message.call_args.kwargs["message_thread_id"] == 33

    async def test_send_files_from_text_passes_thread_id(self, tmp_path: Path) -> None:
        from ductor_bot.bot.sender import send_files_from_text

        f = tmp_path / "data.csv"
        f.write_text("a,b")

        bot = MagicMock()
        bot.send_document = AsyncMock()
        await send_files_from_text(bot, chat_id=1, text=f"<file:{f}>", thread_id=44)
        assert bot.send_document.call_args.kwargs["message_thread_id"] == 44

    async def test_html_fallback_preserves_thread_id(self) -> None:
        from ductor_bot.bot.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock(
            side_effect=[TelegramBadRequest(MagicMock(), "bad HTML"), None],
        )
        await send_rich(bot, chat_id=1, text="test", thread_id=88)
        for call in bot.send_message.call_args_list:
            assert call.kwargs["message_thread_id"] == 88
