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
        from ductor_bot.bot.handlers import handle_abort

        orchestrator = MagicMock()
        orchestrator.abort = AsyncMock(return_value=2)
        bot = MagicMock()
        bot.send_message = AsyncMock()

        msg = _make_message(chat_id=42)
        result = await handle_abort(orchestrator, bot, chat_id=42, message=msg)
        assert result is True
        orchestrator.abort.assert_called_once_with(42)

    async def test_abort_no_orchestrator(self) -> None:
        from ductor_bot.bot.handlers import handle_abort

        msg = _make_message()
        result = await handle_abort(None, MagicMock(), chat_id=1, message=msg)
        assert result is False


class TestHandleCommand:
    """Test orchestrator command dispatching."""

    async def test_command_routes_to_orchestrator(self) -> None:
        from ductor_bot.bot.handlers import handle_command
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
        from ductor_bot.bot.handlers import handle_new_session

        orchestrator = MagicMock()
        orchestrator.reset_active_provider_session = AsyncMock(return_value="claude")
        bot = MagicMock()
        bot.send_message = AsyncMock()

        msg = _make_message(chat_id=1, text="/new")
        await handle_new_session(orchestrator, bot, msg)
        orchestrator.reset_active_provider_session.assert_called_once_with(1)


class TestStripMention:
    """Test @mention removal."""

    def test_removes_mention(self) -> None:
        from ductor_bot.bot.handlers import strip_mention

        assert strip_mention("@mybot hello", "mybot").strip() == "hello"

    def test_no_mention(self) -> None:
        from ductor_bot.bot.handlers import strip_mention

        assert strip_mention("just text", "mybot") == "just text"

    def test_none_username(self) -> None:
        from ductor_bot.bot.handlers import strip_mention

        assert strip_mention("@bot hi", None) == "@bot hi"


class TestForumTopicPropagation:
    """Test that handlers extract and propagate thread_id."""

    @patch("ductor_bot.bot.handlers.send_rich", new_callable=AsyncMock)
    async def test_handle_abort_passes_thread_id(self, mock_send: AsyncMock) -> None:
        from ductor_bot.bot.handlers import handle_abort

        orchestrator = MagicMock()
        orchestrator.abort = AsyncMock(return_value=1)
        orchestrator.active_provider_name = "claude"
        bot = MagicMock()
        msg = _make_message(chat_id=42, topic_thread_id=99)

        await handle_abort(orchestrator, bot, chat_id=42, message=msg)
        assert mock_send.call_args.kwargs["thread_id"] == 99

    @patch("ductor_bot.bot.handlers.send_rich", new_callable=AsyncMock)
    async def test_handle_command_passes_thread_id(self, mock_send: AsyncMock) -> None:
        from ductor_bot.bot.handlers import handle_command
        from ductor_bot.orchestrator.registry import OrchestratorResult

        orchestrator = MagicMock()
        orchestrator.handle_message = AsyncMock(return_value=OrchestratorResult(text="OK"))
        bot = MagicMock()
        msg = _make_message(text="/status", topic_thread_id=77)

        await handle_command(orchestrator, bot, msg)
        assert mock_send.call_args.kwargs["thread_id"] == 77

    @patch("ductor_bot.bot.handlers.send_rich", new_callable=AsyncMock)
    async def test_handle_new_session_passes_thread_id(self, mock_send: AsyncMock) -> None:
        from ductor_bot.bot.handlers import handle_new_session

        orchestrator = MagicMock()
        orchestrator.reset_active_provider_session = AsyncMock(return_value="claude")
        bot = MagicMock()
        msg = _make_message(text="/new", topic_thread_id=55)

        await handle_new_session(orchestrator, bot, msg)
        assert mock_send.call_args.kwargs["thread_id"] == 55

    @patch("ductor_bot.bot.handlers.send_rich", new_callable=AsyncMock)
    async def test_handle_abort_none_thread_id_for_normal_msg(self, mock_send: AsyncMock) -> None:
        from ductor_bot.bot.handlers import handle_abort

        orchestrator = MagicMock()
        orchestrator.abort = AsyncMock(return_value=0)
        orchestrator.active_provider_name = "claude"
        bot = MagicMock()
        msg = _make_message(chat_id=1)

        await handle_abort(orchestrator, bot, chat_id=1, message=msg)
        assert mock_send.call_args.kwargs.get("thread_id") is None
