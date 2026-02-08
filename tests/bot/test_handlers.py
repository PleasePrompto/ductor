"""Tests for bot message/command handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aiogram.types import Message


def _make_message(chat_id: int = 1, user_id: int = 100, text: str = "hello") -> MagicMock:
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
        orchestrator.reset_session = AsyncMock()
        bot = MagicMock()
        bot.send_message = AsyncMock()

        msg = _make_message(chat_id=1, text="/new")
        await handle_new_session(orchestrator, bot, msg)
        orchestrator.reset_session.assert_called_once_with(1)


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
