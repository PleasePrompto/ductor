"""Tests for bot message/command handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import Message


def _make_message(
    chat_id: int = 1,
    user_id: int = 100,
    text: str = "hello",
    *,
    topic_thread_id: int | None = None,
) -> MagicMock:
    """Create a mock aiogram Message."""
    msg = MagicMock(spec=Message)
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = "private"
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.text = text
    msg.message_id = 1
    msg.answer = AsyncMock()
    msg.photo = None
    msg.document = None
    msg.voice = None
    msg.video = None
    msg.audio = None
    msg.sticker = None
    msg.video_note = None
    msg.is_topic_message = topic_thread_id is not None
    msg.message_thread_id = topic_thread_id
    return msg


class TestHandleAbort:
    """Test abort handling logic."""

    async def test_abort_kills_processes_and_replies(self) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_abort

        orchestrator = MagicMock()
        orchestrator.abort = AsyncMock(return_value=2)
        bot = MagicMock()
        bot.send_message = AsyncMock()

        msg = _make_message(chat_id=42)
        result = await handle_abort(orchestrator, bot, chat_id=42, message=msg)
        assert result is True
        orchestrator.abort.assert_called_once_with(42, topic_id=None)

    async def test_abort_zero_killed_sends_nothing_text(self) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_abort
        from ductor_bot.text.response_format import stop_text

        orchestrator = MagicMock()
        orchestrator.abort = AsyncMock(return_value=0)
        orchestrator.active_provider_name = "Claude"
        bot = MagicMock()
        msg = _make_message(chat_id=42)

        with patch(
            "ductor_bot.messenger.telegram.handlers.send_rich",
            new_callable=AsyncMock,
        ) as send_rich:
            result = await handle_abort(orchestrator, bot, chat_id=42, message=msg)

        assert result is True
        send_rich.assert_awaited_once()
        assert send_rich.call_args.args[2] == stop_text(False, "Claude")
        assert "Nothing running right now." in send_rich.call_args.args[2]

    async def test_abort_no_orchestrator(self) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_abort

        msg = _make_message()
        result = await handle_abort(None, MagicMock(), chat_id=1, message=msg)
        assert result is False


class TestHandleAbortAll:
    """Test abort-all handling logic."""

    async def test_abort_all_kills_local_and_callback(self) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_abort_all

        orchestrator = MagicMock()
        orchestrator.abort_all = AsyncMock(return_value=2)
        callback = AsyncMock(return_value=3)
        bot = MagicMock()
        bot.send_message = AsyncMock()

        msg = _make_message(chat_id=42)
        result = await handle_abort_all(
            orchestrator,
            bot,
            chat_id=42,
            message=msg,
            abort_all_callback=callback,
        )
        assert result is True
        orchestrator.abort_all.assert_called_once()
        callback.assert_called_once()

    async def test_abort_all_no_callback(self) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_abort_all

        orchestrator = MagicMock()
        orchestrator.abort_all = AsyncMock(return_value=1)
        bot = MagicMock()
        bot.send_message = AsyncMock()

        msg = _make_message(chat_id=42)
        result = await handle_abort_all(
            orchestrator,
            bot,
            chat_id=42,
            message=msg,
            abort_all_callback=None,
        )
        assert result is True
        orchestrator.abort_all.assert_called_once()

    async def test_abort_all_no_orchestrator(self) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_abort_all

        msg = _make_message()
        result = await handle_abort_all(None, MagicMock(), chat_id=1, message=msg)
        assert result is False

    async def test_abort_all_zero_killed(self) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_abort_all

        orchestrator = MagicMock()
        orchestrator.abort_all = AsyncMock(return_value=0)
        callback = AsyncMock(return_value=0)
        bot = MagicMock()
        bot.send_message = AsyncMock()

        msg = _make_message(chat_id=42)
        result = await handle_abort_all(
            orchestrator,
            bot,
            chat_id=42,
            message=msg,
            abort_all_callback=callback,
        )
        assert result is True


class TestHandleCommand:
    """Test orchestrator command dispatching."""

    async def test_command_routes_to_orchestrator(self) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_command
        from ductor_bot.orchestrator.registry import OrchestratorResult

        orchestrator = MagicMock()
        orchestrator.handle_message = AsyncMock(return_value=OrchestratorResult(text="Status: OK"))
        bot = MagicMock()
        bot.send_message = AsyncMock()

        msg = _make_message(text="/status")
        await handle_command(orchestrator, bot, msg)
        orchestrator.handle_message.assert_called_once()


class TestHandleNewSession:
    """Test /new handler logic."""

    async def test_new_resets_session(self) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_new_session

        orchestrator = MagicMock()
        orchestrator.reset_active_provider_session = AsyncMock(return_value="claude")
        bot = MagicMock()
        bot.send_message = AsyncMock()

        msg = _make_message(chat_id=1, text="/new")
        await handle_new_session(orchestrator, bot, msg)
        from ductor_bot.session.key import SessionKey

        orchestrator.reset_active_provider_session.assert_called_once_with(SessionKey(chat_id=1))


class TestStripMention:
    """Test @mention removal."""

    def test_removes_mention(self) -> None:
        from ductor_bot.messenger.telegram.handlers import strip_mention

        assert strip_mention("@mybot hello", "mybot").strip() == "hello"

    def test_no_mention(self) -> None:
        from ductor_bot.messenger.telegram.handlers import strip_mention

        assert strip_mention("just text", "mybot") == "just text"

    def test_none_username(self) -> None:
        from ductor_bot.messenger.telegram.handlers import strip_mention

        assert strip_mention("@bot hi", None) == "@bot hi"


class TestBuildReplyPrompt:
    """Reply-context prompt construction for Telegram replies (#135)."""

    @staticmethod
    def _message(
        *,
        quote_text: str | None = None,
        reply_text: str | None = None,
        reply_caption: str | None = None,
        has_reply: bool = False,
    ) -> Message:
        message = MagicMock(spec=Message)
        message.quote = MagicMock(text=quote_text) if quote_text is not None else None
        if has_reply or reply_text is not None or reply_caption is not None:
            message.reply_to_message = MagicMock(text=reply_text, caption=reply_caption)
        else:
            message.reply_to_message = None
        return message

    def test_quote_fragment_preferred(self) -> None:
        from ductor_bot.messenger.telegram.handlers import build_reply_prompt

        msg = self._message(quote_text="point 2", reply_text="the full brief")
        prompt = build_reply_prompt(msg, "expand on this")
        assert "> point 2" in prompt
        assert "the full brief" not in prompt
        assert prompt.endswith("The user's message:\nexpand on this")

    def test_reply_text_is_quoted_and_labeled(self) -> None:
        from ductor_bot.messenger.telegram.handlers import build_reply_prompt

        msg = self._message(reply_text="line one\nline two")
        prompt = build_reply_prompt(msg, "go on")
        assert "The user is replying to this quoted message:\n> line one\n> line two" in prompt
        assert prompt.endswith("The user's message:\ngo on")

    def test_caption_used_when_no_text(self) -> None:
        from ductor_bot.messenger.telegram.handlers import build_reply_prompt

        prompt = build_reply_prompt(self._message(reply_caption="a photo caption"), "what is this")
        assert "> a photo caption" in prompt

    def test_no_reply_returns_text_unchanged(self) -> None:
        from ductor_bot.messenger.telegram.handlers import build_reply_prompt

        assert build_reply_prompt(self._message(), "hello") == "hello"

    def test_service_message_without_text_unchanged(self) -> None:
        from ductor_bot.messenger.telegram.handlers import build_reply_prompt

        # Forum-topic service messages carry neither text nor caption.
        assert build_reply_prompt(self._message(has_reply=True), "hi") == "hi"


class TestPrependReplyToMedia:
    """Reply-context prefixing for non-text (media) replies (#135)."""

    _MEDIA_ATTRS = ("photo", "document", "voice", "audio", "video", "video_note", "sticker")

    @classmethod
    def _message(cls, *, reply_text: str | None = None, kind: str | None = None) -> Message:
        message = MagicMock(spec=Message)
        message.quote = None
        message.reply_to_message = (
            MagicMock(text=reply_text, caption=None) if reply_text is not None else None
        )
        for attr in cls._MEDIA_ATTRS:
            setattr(message, attr, "x" if attr == kind else None)
        return message

    def test_prefixes_quote_and_labels_voice(self) -> None:
        from ductor_bot.messenger.telegram.handlers import prepend_reply_to_media

        msg = self._message(reply_text="Point 3: deploy Friday", kind="voice")
        out = prepend_reply_to_media(msg, "[INCOMING FILE]\n...")
        assert "The user is replying to this quoted message:\n> Point 3: deploy Friday" in out
        assert "Their reply is a voice message (the attached file below)." in out
        assert out.endswith("[INCOMING FILE]\n...")

    def test_labels_image(self) -> None:
        from ductor_bot.messenger.telegram.handlers import prepend_reply_to_media

        msg = self._message(reply_text="the brief", kind="photo")
        assert "Their reply is an image" in prepend_reply_to_media(msg, "BODY")

    def test_labels_video(self) -> None:
        from ductor_bot.messenger.telegram.handlers import prepend_reply_to_media

        msg = self._message(reply_text="the brief", kind="video")
        assert "Their reply is a video" in prepend_reply_to_media(msg, "BODY")

    def test_no_reply_returns_media_prompt_unchanged(self) -> None:
        from ductor_bot.messenger.telegram.handlers import prepend_reply_to_media

        assert prepend_reply_to_media(self._message(kind="voice"), "BODY") == "BODY"


class TestForumTopicPropagation:
    """Test that handlers extract and propagate thread_id."""

    @patch("ductor_bot.messenger.telegram.handlers.send_rich", new_callable=AsyncMock)
    async def test_abort_entrypoint_passes_topic_id(self, _mock_send: AsyncMock) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_abort

        orchestrator = MagicMock()
        orchestrator.abort = AsyncMock(return_value=1)
        orchestrator.active_provider_name = "claude"
        bot = MagicMock()
        msg = _make_message(chat_id=42, topic_thread_id=99)

        await handle_abort(orchestrator, bot, chat_id=42, message=msg)
        orchestrator.abort.assert_called_once_with(42, topic_id=99)

    @patch("ductor_bot.messenger.telegram.handlers.send_rich", new_callable=AsyncMock)
    async def test_handle_abort_passes_thread_id(self, mock_send: AsyncMock) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_abort

        orchestrator = MagicMock()
        orchestrator.abort = AsyncMock(return_value=1)
        orchestrator.active_provider_name = "claude"
        bot = MagicMock()
        msg = _make_message(chat_id=42, topic_thread_id=99)

        await handle_abort(orchestrator, bot, chat_id=42, message=msg)
        opts = mock_send.call_args[0][3]
        assert opts.thread_id == 99

    @patch("ductor_bot.messenger.telegram.handlers.send_rich", new_callable=AsyncMock)
    async def test_handle_command_passes_thread_id(self, mock_send: AsyncMock) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_command
        from ductor_bot.orchestrator.registry import OrchestratorResult

        orchestrator = MagicMock()
        orchestrator.handle_message = AsyncMock(return_value=OrchestratorResult(text="OK"))
        bot = MagicMock()
        msg = _make_message(text="/status", topic_thread_id=77)

        await handle_command(orchestrator, bot, msg)
        opts = mock_send.call_args[0][3]
        assert opts.thread_id == 77

    @patch("ductor_bot.messenger.telegram.handlers.send_rich", new_callable=AsyncMock)
    async def test_handle_new_session_passes_thread_id(self, mock_send: AsyncMock) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_new_session

        orchestrator = MagicMock()
        orchestrator.reset_active_provider_session = AsyncMock(return_value="claude")
        bot = MagicMock()
        msg = _make_message(text="/new", topic_thread_id=55)

        await handle_new_session(orchestrator, bot, msg)
        opts = mock_send.call_args[0][3]
        assert opts.thread_id == 55

    @patch("ductor_bot.messenger.telegram.handlers.send_rich", new_callable=AsyncMock)
    async def test_handle_abort_none_thread_id_for_normal_msg(self, mock_send: AsyncMock) -> None:
        from ductor_bot.messenger.telegram.handlers import handle_abort

        orchestrator = MagicMock()
        orchestrator.abort = AsyncMock(return_value=0)
        orchestrator.active_provider_name = "claude"
        bot = MagicMock()
        msg = _make_message(chat_id=1)

        await handle_abort(orchestrator, bot, chat_id=1, message=msg)
        opts = mock_send.call_args[0][3]
        assert opts.thread_id is None
