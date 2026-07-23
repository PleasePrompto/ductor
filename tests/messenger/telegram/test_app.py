"""Tests for TelegramBot app setup and handler methods."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Chat, Message, User

from ductor_bot.config import AgentConfig, StreamingConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    streaming_enabled: bool = True,
    user_ids: list[int] | None = None,
    group_mention_only: bool = False,
) -> AgentConfig:
    return AgentConfig(
        telegram_token="test:token",
        allowed_user_ids=user_ids or [100],
        streaming=StreamingConfig(enabled=streaming_enabled),
        group_mention_only=group_mention_only,
    )


def _make_tg_bot(
    config: AgentConfig | None = None,
    *,
    bot_instance: MagicMock | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Create a TelegramBot with mocked Bot and Orchestrator.

    Returns ``(tg_bot, bot_instance)`` where *tg_bot* is the TelegramBot and
    *bot_instance* is the mocked aiogram Bot for assertion.
    """
    from ductor_bot.messenger.telegram.app import TelegramBot

    cfg = config or _make_config()
    if bot_instance is None:
        bot_instance = MagicMock()
        bot_instance.edit_message_reply_markup = AsyncMock()
        bot_instance.edit_message_text = AsyncMock()
        bot_instance.send_message = AsyncMock()
        bot_instance.send_photo = AsyncMock()
        bot_instance.send_chat_action = AsyncMock()
        bot_instance.delete_webhook = AsyncMock()

    with patch("ductor_bot.messenger.telegram.app.Bot", return_value=bot_instance):
        tg_bot = TelegramBot(cfg)

    return tg_bot, bot_instance  # type: ignore[return-value]


def _make_orchestrator(
    *,
    handle_message_text: str = "Response",
    handle_streaming_text: str = "Streamed",
    stream_fallback: bool = True,
) -> MagicMock:
    orch = MagicMock()
    orch.handle_message = AsyncMock(
        return_value=MagicMock(text=handle_message_text, buttons=None),
    )
    orch.handle_message_streaming = AsyncMock(
        return_value=MagicMock(text=handle_streaming_text, stream_fallback=stream_fallback),
    )
    orch.abort = AsyncMock(return_value=1)
    orch.shutdown = AsyncMock()

    paths = MagicMock()
    paths.workspace = Path("/tmp/test-workspace")
    paths.ductor_home = Path("/tmp/test-ductor")
    paths.telegram_files_dir = Path("/tmp/test-workspace/telegram_files")
    orch.paths = paths
    return orch


def _make_message(
    chat_id: int = 1,
    message_id: int = 10,
    text: str | None = "Hello",
    chat_type: str = "private",
    user_id: int = 100,
    *,
    topic_thread_id: int | None = None,
) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = chat_id
    msg.chat.type = chat_type
    type(msg).message_id = PropertyMock(return_value=message_id)
    msg.text = text
    msg.answer = AsyncMock(return_value=msg)

    user = MagicMock(spec=User)
    user.id = user_id
    user.first_name = "TestUser"
    msg.from_user = user

    # Media defaults: no media
    msg.photo = None
    msg.document = None
    msg.voice = None
    msg.video = None
    msg.audio = None
    msg.sticker = None
    msg.video_note = None

    # Reply defaults: not a reply
    msg.quote = None
    msg.reply_to_message = None

    # Forum topic support
    msg.is_topic_message = topic_thread_id is not None
    msg.message_thread_id = topic_thread_id

    return msg


def _make_callback_query(
    data: str = "Yes",
    chat_id: int = 1,
    message_id: int = 42,
    user_id: int = 100,
    msg_text: str | None = "Bot response",
    msg_html_text: str | None = "Bot response",
    *,
    topic_thread_id: int | None = None,
) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.answer = AsyncMock()

    msg = MagicMock(spec=Message)
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = chat_id
    msg.chat.type = "private"
    type(msg).message_id = PropertyMock(return_value=message_id)
    msg.text = msg_text
    msg.html_text = msg_html_text
    msg.answer = AsyncMock(return_value=msg)
    msg.is_topic_message = topic_thread_id is not None
    msg.message_thread_id = topic_thread_id
    cb.message = msg

    user = MagicMock(spec=User)
    user.id = user_id
    cb.from_user = user

    return cb


# ---------------------------------------------------------------------------
# Init & lifecycle (existing)
# ---------------------------------------------------------------------------


class TestTelegramBotInit:
    def test_creates_dispatcher_and_router(self) -> None:
        tg_bot, _ = _make_tg_bot()
        assert tg_bot._dp is not None
        assert tg_bot._router is not None

    def test_registers_command_handlers(self) -> None:
        tg_bot, _ = _make_tg_bot()
        assert len(tg_bot._dp.sub_routers) > 0

    def test_registers_callback_query_handler(self) -> None:
        tg_bot, _ = _make_tg_bot()
        assert len(tg_bot._router.callback_query.handlers) > 0

    def test_orch_property_raises_before_startup(self) -> None:
        tg_bot, _ = _make_tg_bot()
        with pytest.raises(RuntimeError, match="Orchestrator not initialized"):
            _ = tg_bot._orch


class TestTelegramBotRun:
    async def test_run_returns_exit_code(self) -> None:
        tg_bot, bot_instance = _make_tg_bot()
        tg_bot._dp.resolve_used_update_types = MagicMock(return_value=["message", "callback_query"])
        tg_bot._dp.start_polling = AsyncMock()
        code = await tg_bot.run()
        bot_instance.delete_webhook.assert_called_once_with(drop_pending_updates=True)
        tg_bot._dp.start_polling.assert_called_once_with(
            bot_instance,
            allowed_updates=["message", "callback_query"],
            close_bot_session=True,
            handle_signals=False,
        )
        assert code == 0

    async def test_run_disables_aiogram_signal_handling(self) -> None:
        """start_polling must receive handle_signals=False so that aiogram
        does not overwrite the supervisor's SIGINT/SIGTERM handler."""
        tg_bot, _bot_instance = _make_tg_bot()
        tg_bot._dp.resolve_used_update_types = MagicMock(return_value=["message"])
        tg_bot._dp.start_polling = AsyncMock()
        await tg_bot.run()
        kwargs = tg_bot._dp.start_polling.call_args.kwargs
        assert kwargs.get("handle_signals") is False

    async def test_shutdown_cleans_up(self) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot._orchestrator = MagicMock()
        tg_bot._orchestrator.shutdown = AsyncMock()
        await tg_bot.shutdown()
        tg_bot._orchestrator.shutdown.assert_called_once()

    async def test_shutdown_cancels_restart_watcher(self) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot._orchestrator = MagicMock()
        tg_bot._orchestrator.shutdown = AsyncMock()

        # Create a real long-running task to verify cancellation
        tg_bot._restart_watcher = asyncio.create_task(asyncio.sleep(100))
        await tg_bot.shutdown()
        assert tg_bot._restart_watcher.cancelled()

    async def test_shutdown_releases_polling_session(self) -> None:
        """shutdown() must stop polling, delete webhook, and close the bot session
        so that a restarted instance doesn't hit TelegramConflictError."""
        tg_bot, bot_instance = _make_tg_bot()
        tg_bot._orchestrator = MagicMock()
        tg_bot._orchestrator.shutdown = AsyncMock()

        tg_bot._dp.stop_polling = AsyncMock()
        bot_instance.session = MagicMock()
        bot_instance.session.close = AsyncMock()

        await tg_bot.shutdown()

        tg_bot._dp.stop_polling.assert_called_once()
        bot_instance.delete_webhook.assert_called_once_with(drop_pending_updates=False)
        bot_instance.session.close.assert_called_once()


# ---------------------------------------------------------------------------
# _cancel_task
# ---------------------------------------------------------------------------


class TestCancelTask:
    async def test_cancel_task_none(self) -> None:
        from ductor_bot.messenger.telegram.app import _cancel_task

        await _cancel_task(None)

    async def test_cancel_task_done(self) -> None:
        from ductor_bot.messenger.telegram.app import _cancel_task

        task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0))
        await task
        await _cancel_task(task)

    async def test_cancel_task_running(self) -> None:
        from ductor_bot.messenger.telegram.app import _cancel_task

        task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(100))
        await _cancel_task(task)
        assert task.cancelled()


# ---------------------------------------------------------------------------
# _on_help
# ---------------------------------------------------------------------------


class TestOnHelp:
    @patch("ductor_bot.messenger.telegram.app.send_rich", new_callable=AsyncMock)
    async def test_sends_help_text(self, mock_send: AsyncMock) -> None:
        tg_bot, bot_instance = _make_tg_bot()
        msg = _make_message(chat_id=42)

        await tg_bot._on_help(msg)

        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs[0][0] is bot_instance
        assert call_kwargs[0][1] == 42
        text = call_kwargs[0][2]
        assert "Command Reference" in text
        assert "/help" in text

    @patch("ductor_bot.messenger.telegram.app.send_rich", new_callable=AsyncMock)
    async def test_help_lists_all_registered_commands(self, mock_send: AsyncMock) -> None:
        from ductor_bot.commands import BOT_COMMANDS

        tg_bot, _bot_instance = _make_tg_bot()
        msg = _make_message(chat_id=42)

        await tg_bot._on_help(msg)

        text = mock_send.call_args[0][2]
        for command, _desc in BOT_COMMANDS:
            assert f"/{command}" in text

    @patch("ductor_bot.messenger.telegram.app.send_rich", new_callable=AsyncMock)
    async def test_help_passes_reply_to(self, mock_send: AsyncMock) -> None:
        tg_bot, _ = _make_tg_bot()
        msg = _make_message()

        await tg_bot._on_help(msg)

        opts = mock_send.call_args[0][3]
        assert opts.reply_to_message_id == msg.message_id


# ---------------------------------------------------------------------------
# _on_start / _show_welcome
# ---------------------------------------------------------------------------


class TestOnStart:
    @patch("ductor_bot.messenger.telegram.app.send_rich", new_callable=AsyncMock)
    @patch("ductor_bot.messenger.telegram.app.build_welcome_keyboard")
    @patch("ductor_bot.messenger.telegram.app.build_welcome_text", return_value="Welcome!")
    @patch("ductor_bot.cli.auth.check_all_auth", return_value={})
    async def test_start_shows_welcome_without_image(
        self,
        _mock_auth: MagicMock,
        mock_text: MagicMock,
        mock_kb: MagicMock,
        mock_send: AsyncMock,
    ) -> None:
        tg_bot, _ = _make_tg_bot()
        msg = _make_message(chat_id=5)

        with patch.object(type(tg_bot), "_send_welcome_image", new_callable=AsyncMock) as mock_img:
            mock_img.return_value = False
            await tg_bot._on_start(msg)

        mock_send.assert_called_once()
        assert mock_send.call_args[0][1] == 5
        assert mock_send.call_args[0][2] == "Welcome!"

    @patch("ductor_bot.cli.auth.check_all_auth", return_value={})
    async def test_start_sends_image_when_available(self, _mock_auth: MagicMock) -> None:
        tg_bot, _bot_instance = _make_tg_bot()
        msg = _make_message(chat_id=5)

        with patch.object(type(tg_bot), "_send_welcome_image", new_callable=AsyncMock) as mock_img:
            mock_img.return_value = True
            await tg_bot._on_start(msg)
            mock_img.assert_called_once()


# ---------------------------------------------------------------------------
# _send_welcome_image
# ---------------------------------------------------------------------------


class TestSendWelcomeImage:
    async def test_returns_false_when_no_image_file(self) -> None:
        tg_bot, _ = _make_tg_bot()
        msg = _make_message()
        kb = MagicMock()

        with patch(
            "ductor_bot.messenger.telegram.app._WELCOME_IMAGE", Path("/nonexistent/file.png")
        ):
            result = await tg_bot._send_welcome_image(1, "text", kb, msg)

        assert result is False

    async def test_returns_true_with_short_caption(self) -> None:
        tg_bot, bot_instance = _make_tg_bot()
        msg = _make_message()
        kb = MagicMock()
        short_text = "Hi"

        with (
            patch("ductor_bot.messenger.telegram.app._WELCOME_IMAGE") as mock_path,
            patch(
                "ductor_bot.messenger.telegram.app.markdown_to_telegram_html",
                return_value="<b>Hi</b>",
            ),
        ):
            mock_path.is_file.return_value = True
            result = await tg_bot._send_welcome_image(1, short_text, kb, msg)

        assert result is True
        bot_instance.send_photo.assert_called_once()

    async def test_returns_false_on_bad_request(self) -> None:
        tg_bot, bot_instance = _make_tg_bot()
        msg = _make_message()
        kb = MagicMock()

        bot_instance.send_photo = AsyncMock(
            side_effect=[TelegramBadRequest(method=MagicMock(), message="bad"), None]
        )

        with patch("ductor_bot.messenger.telegram.app._WELCOME_IMAGE") as mock_path:
            mock_path.is_file.return_value = True
            result = await tg_bot._send_welcome_image(1, "x", kb, msg)

        assert result is False
        assert bot_instance.send_photo.call_count == 2

    async def test_returns_false_on_generic_exception(self) -> None:
        tg_bot, bot_instance = _make_tg_bot()
        msg = _make_message()
        kb = MagicMock()

        bot_instance.send_photo = AsyncMock(side_effect=OSError("disk error"))

        with patch("ductor_bot.messenger.telegram.app._WELCOME_IMAGE") as mock_path:
            mock_path.is_file.return_value = True
            result = await tg_bot._send_welcome_image(1, "x", kb, msg)

        assert result is False


# ---------------------------------------------------------------------------
# _on_restart
# ---------------------------------------------------------------------------


class TestOnRestart:
    @patch("ductor_bot.messenger.telegram.app.send_rich", new_callable=AsyncMock)
    async def test_restart_writes_sentinel_and_stops(self, mock_send: AsyncMock) -> None:
        from ductor_bot.infra.restart import EXIT_RESTART

        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch
        tg_bot._dp.stop_polling = AsyncMock()
        msg = _make_message(chat_id=77)

        with patch("ductor_bot.infra.restart.write_restart_sentinel") as mock_sentinel:
            await tg_bot._on_restart(msg)

        mock_send.assert_called_once()
        text = mock_send.call_args[0][2]
        assert "Restarting" in text
        assert tg_bot._exit_code == EXIT_RESTART
        mock_sentinel.assert_called_once()
        sentinel_kwargs = mock_sentinel.call_args
        assert sentinel_kwargs[0][0] == 77


# ---------------------------------------------------------------------------
# _on_message
# ---------------------------------------------------------------------------


class TestOnMessage:
    async def test_routes_text_to_non_streaming(self) -> None:
        config = _make_config(streaming_enabled=False)
        tg_bot, _bot_instance = _make_tg_bot(config)
        orch = _make_orchestrator(handle_message_text="Non-streamed reply")
        tg_bot._orchestrator = orch

        msg = _make_message(text="Hello bot")

        with patch(
            "ductor_bot.messenger.telegram.app.run_non_streaming_message", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = "Non-streamed reply"
            await tg_bot._on_message(msg)

        mock_run.assert_awaited_once()
        dispatch = mock_run.call_args.args[0]
        assert dispatch.key.chat_id == 1
        assert dispatch.text == "Hello bot"
        assert dispatch.reply_to is msg

    async def test_routes_text_to_streaming(self) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        msg = _make_message(text="Hello streaming")

        with patch.object(tg_bot, "_handle_streaming", new_callable=AsyncMock) as mock_stream:
            await tg_bot._on_message(msg)

        from ductor_bot.session.key import SessionKey

        mock_stream.assert_called_once_with(
            msg, SessionKey(chat_id=1), "Hello streaming", thread_id=None
        )

    async def test_returns_early_for_none_text(self) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        msg = _make_message(text=None)
        await tg_bot._on_message(msg)

        orch.handle_message.assert_not_called()
        orch.handle_message_streaming.assert_not_called()

    @patch("ductor_bot.messenger.telegram.app.strip_mention", return_value="clean text")
    async def test_strips_mention_from_text(self, mock_strip: MagicMock) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot.bot_instance_username = "testbot"
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        msg = _make_message(text="@testbot clean text")

        with patch.object(tg_bot, "_handle_streaming", new_callable=AsyncMock) as mock_stream:
            await tg_bot._on_message(msg)

        from ductor_bot.session.key import SessionKey

        mock_stream.assert_called_once_with(
            msg, SessionKey(chat_id=1), "clean text", thread_id=None
        )


# ---------------------------------------------------------------------------
# _resolve_text
# ---------------------------------------------------------------------------


class TestResolveText:
    async def test_plain_text_message(self) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot.bot_instance_username = "mybot"
        tg_bot._orchestrator = _make_orchestrator()
        msg = _make_message(text="Hello")
        result = await tg_bot._resolve_text(msg)
        assert result == "Hello"

    async def test_none_when_no_text_and_no_media(self) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()
        msg = _make_message(text=None)
        result = await tg_bot._resolve_text(msg)
        assert result is None

    @patch("ductor_bot.messenger.telegram.app.resolve_media_text", new_callable=AsyncMock)
    @patch("ductor_bot.messenger.telegram.app.has_media", return_value=True)
    async def test_media_in_private_chat(
        self, _mock_has: MagicMock, mock_resolve: AsyncMock
    ) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()
        msg = _make_message(chat_type="private")
        mock_resolve.return_value = "[MEDIA PROMPT]"

        result = await tg_bot._resolve_text(msg)
        assert result == "[MEDIA PROMPT]"

    @patch("ductor_bot.messenger.telegram.app.should_drop_in_group", return_value=True)
    @patch("ductor_bot.messenger.telegram.app.has_media", return_value=True)
    async def test_media_in_group_not_addressed_mention_only(
        self, _mock_has: MagicMock, _mock_drop: MagicMock
    ) -> None:
        cfg = _make_config(group_mention_only=True)
        tg_bot, _ = _make_tg_bot(cfg)
        tg_bot._orchestrator = _make_orchestrator()
        msg = _make_message(chat_type="group")

        result = await tg_bot._resolve_text(msg)
        assert result is None

    @patch("ductor_bot.messenger.telegram.app.resolve_media_text", new_callable=AsyncMock)
    @patch("ductor_bot.messenger.telegram.app.has_media", return_value=True)
    async def test_media_in_group_not_addressed_mention_not_required(
        self, _mock_has: MagicMock, mock_resolve: AsyncMock
    ) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()
        msg = _make_message(chat_type="group")
        mock_resolve.return_value = "[MEDIA]"

        result = await tg_bot._resolve_text(msg)
        assert result == "[MEDIA]"

    @patch("ductor_bot.messenger.telegram.app.resolve_media_text", new_callable=AsyncMock)
    @patch("ductor_bot.messenger.telegram.app.should_drop_in_group", return_value=False)
    @patch("ductor_bot.messenger.telegram.app.has_media", return_value=True)
    async def test_media_in_group_addressed(
        self, _mock_has: MagicMock, _mock_drop: MagicMock, mock_resolve: AsyncMock
    ) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()
        msg = _make_message(chat_type="supergroup")
        mock_resolve.return_value = "[MEDIA]"

        result = await tg_bot._resolve_text(msg)
        assert result == "[MEDIA]"

    @patch("ductor_bot.messenger.telegram.app.is_command_for_others", return_value=True)
    @patch("ductor_bot.messenger.telegram.app.has_media", return_value=True)
    async def test_media_in_group_for_other_bot(
        self, _mock_has: MagicMock, _mock_others: MagicMock
    ) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()
        msg = _make_message(chat_type="group")

        result = await tg_bot._resolve_text(msg)
        assert result is None


# ---------------------------------------------------------------------------
# _handle_streaming
# ---------------------------------------------------------------------------


class TestHandleStreaming:
    async def test_streaming_fallback_sends_rich(self) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator(handle_streaming_text="Fallback", stream_fallback=True)
        tg_bot._orchestrator = orch

        msg = _make_message()

        from ductor_bot.session.key import SessionKey

        key = SessionKey(chat_id=1)
        with patch(
            "ductor_bot.messenger.telegram.app.run_streaming_message", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = "Fallback"
            await tg_bot._handle_streaming(msg, key, "test")

        mock_run.assert_awaited_once()
        dispatch = mock_run.call_args.args[0]
        assert dispatch.key.chat_id == 1
        assert dispatch.text == "test"
        assert dispatch.message is msg

    async def test_streaming_success_sends_files_only(self) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator(
            handle_streaming_text="Streamed <file:/tmp/out.png>", stream_fallback=False
        )
        tg_bot._orchestrator = orch

        msg = _make_message()

        from ductor_bot.session.key import SessionKey

        key = SessionKey(chat_id=1)
        with patch(
            "ductor_bot.messenger.telegram.app.run_streaming_message", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = "Streamed <file:/tmp/out.png>"
            await tg_bot._handle_streaming(msg, key, "test")

        mock_run.assert_awaited_once()
        dispatch = mock_run.call_args.args[0]
        assert dispatch.key.chat_id == 1
        assert dispatch.text == "test"


# ---------------------------------------------------------------------------
# _on_callback_query (extended)
# ---------------------------------------------------------------------------


class TestCallbackQueryHandler:
    async def test_callback_answers_to_dismiss_spinner(self) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()

        cb = _make_callback_query(data="Yes")
        await tg_bot._on_callback_query(cb)
        cb.answer.assert_called_once()

    async def test_callback_appends_user_answer_indicator(self) -> None:
        tg_bot, bot_instance = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()

        cb = _make_callback_query(data="No", message_id=99)
        await tg_bot._on_callback_query(cb)

        bot_instance.edit_message_text.assert_called_once()
        call_kwargs = bot_instance.edit_message_text.call_args
        assert call_kwargs.kwargs["chat_id"] == 1
        assert call_kwargs.kwargs["message_id"] == 99
        assert call_kwargs.kwargs["reply_markup"] is None
        assert "[USER ANSWER] No" in call_kwargs.kwargs["text"]

    async def test_callback_indicator_fallback_on_bad_request(self) -> None:
        tg_bot, bot_instance = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()

        bot_instance.edit_message_text = AsyncMock(
            side_effect=TelegramBadRequest(method=MagicMock(), message="too long")
        )
        cb = _make_callback_query(data="Click", message_id=50)
        await tg_bot._on_callback_query(cb)

        bot_instance.edit_message_reply_markup.assert_called_once_with(
            chat_id=1, message_id=50, reply_markup=None
        )

    async def test_callback_indicator_caption_message_falls_back(self) -> None:
        tg_bot, bot_instance = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()

        cb = _make_callback_query(data="Click", message_id=60, msg_text=None, msg_html_text=None)
        await tg_bot._on_callback_query(cb)

        bot_instance.edit_message_text.assert_not_called()
        bot_instance.edit_message_reply_markup.assert_called_once_with(
            chat_id=1, message_id=60, reply_markup=None
        )

    async def test_callback_sends_button_text_to_orchestrator(self) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        cb = _make_callback_query(data="Approve")
        await tg_bot._on_callback_query(cb)

        from ductor_bot.session.key import SessionKey

        orch.handle_message_streaming.assert_called_once()
        call_args = orch.handle_message_streaming.call_args
        assert call_args[0][0] == SessionKey(chat_id=1)
        assert call_args[0][1] == "Approve"

    async def test_callback_ignores_empty_data(self) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        cb = _make_callback_query(data="")
        cb.data = ""
        await tg_bot._on_callback_query(cb)
        cb.answer.assert_called_once()
        orch.handle_message_streaming.assert_not_called()

    async def test_callback_ignores_no_message(self) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        cb = _make_callback_query()
        cb.message = None
        await tg_bot._on_callback_query(cb)
        cb.answer.assert_called_once()
        orch.handle_message_streaming.assert_not_called()

    async def test_model_selector_callback_edits_message(self) -> None:
        tg_bot, bot_instance = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        from ductor_bot.orchestrator.selectors.models import (
            Button,
            ButtonGrid,
            SelectorResponse,
        )

        resp = SelectorResponse(
            text="Select Claude model:",
            buttons=ButtonGrid(rows=[[Button(text="OPUS", callback_data="ms:m:opus")]]),
        )
        with patch(
            "ductor_bot.orchestrator.selectors.model_selector.handle_model_callback",
            new_callable=AsyncMock,
            return_value=resp,
        ):
            cb = _make_callback_query(data="ms:p:claude", message_id=55)
            await tg_bot._on_callback_query(cb)

        bot_instance.edit_message_text.assert_called_once()
        orch.handle_message_streaming.assert_not_called()

    async def test_cron_selector_callback_edits_message(self) -> None:
        tg_bot, bot_instance = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        from ductor_bot.orchestrator.selectors.models import (
            Button,
            ButtonGrid,
            SelectorResponse,
        )

        resp = SelectorResponse(
            text="Scheduled Tasks",
            buttons=ButtonGrid(rows=[[Button(text="Refresh", callback_data="crn:r:0")]]),
        )
        with patch(
            "ductor_bot.orchestrator.selectors.cron_selector.handle_cron_callback",
            new_callable=AsyncMock,
            return_value=resp,
        ):
            cb = _make_callback_query(data="crn:r:0", message_id=56)
            await tg_bot._on_callback_query(cb)

        bot_instance.edit_message_text.assert_called_once()
        orch.handle_message_streaming.assert_not_called()

    async def test_non_model_selector_callback_routes_normally(self) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        cb = _make_callback_query(data="Approve")
        await tg_bot._on_callback_query(cb)
        orch.handle_message_streaming.assert_called_once()

    async def test_callback_inaccessible_message_ignored(self) -> None:
        from aiogram.types import InaccessibleMessage

        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        cb = _make_callback_query(data="test")
        inaccessible = MagicMock(spec=InaccessibleMessage)
        cb.message = inaccessible
        await tg_bot._on_callback_query(cb)
        orch.handle_message_streaming.assert_not_called()

    async def test_callback_non_streaming_routes_to_handle_message(self) -> None:
        config = _make_config(streaming_enabled=False)
        tg_bot, _ = _make_tg_bot(config)
        orch = _make_orchestrator(handle_message_text="Non-streamed")
        tg_bot._orchestrator = orch

        cb = _make_callback_query(data="Approve")

        with patch(
            "ductor_bot.messenger.telegram.app.run_non_streaming_message", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = "Non-streamed"
            await tg_bot._on_callback_query(cb)

        mock_run.assert_awaited_once()
        dispatch = mock_run.call_args.args[0]
        assert dispatch.key.chat_id == 1
        assert dispatch.text == "Approve"

    async def test_welcome_callback_resolves_to_prompt(self) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        cb = _make_callback_query(data="w:1")
        await tg_bot._on_callback_query(cb)

        orch.handle_message_streaming.assert_called_once()
        sent_text = orch.handle_message_streaming.call_args[0][1]
        assert "set up ductor.dev" in sent_text

    async def test_welcome_callback_shows_button_label_in_indicator(self) -> None:
        tg_bot, bot_instance = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()

        cb = _make_callback_query(data="w:1")
        await tg_bot._on_callback_query(cb)

        call_kwargs = bot_instance.edit_message_text.call_args
        text = call_kwargs.kwargs["text"]
        assert "Let&#x27;s get to know each other!" in text
        assert "w:1" not in text

    async def test_welcome_callback_unknown_key_returns_early(self) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        cb = _make_callback_query(data="w:unknown")
        await tg_bot._on_callback_query(cb)
        orch.handle_message_streaming.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_model_selector
# ---------------------------------------------------------------------------


class TestHandleModelSelector:
    async def test_edits_message_in_place(self) -> None:
        from ductor_bot.orchestrator.selectors.models import (
            Button,
            ButtonGrid,
            SelectorResponse,
        )
        from ductor_bot.session.key import SessionKey

        tg_bot, bot_instance = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()
        grid = ButtonGrid(rows=[[Button(text="OPUS", callback_data="ms:m:opus")]])
        resp = SelectorResponse(text="Pick a model:", buttons=grid)
        key = SessionKey(chat_id=1)

        with patch(
            "ductor_bot.orchestrator.selectors.model_selector.handle_model_callback",
            new_callable=AsyncMock,
            return_value=resp,
        ):
            await tg_bot._handle_model_selector(key, message_id=50, data="ms:p:claude")

        from aiogram.enums import ParseMode

        call_kwargs = bot_instance.edit_message_text.call_args
        assert call_kwargs is not None
        assert call_kwargs.kwargs["text"] == "Pick a model:"
        assert call_kwargs.kwargs["chat_id"] == 1
        assert call_kwargs.kwargs["message_id"] == 50
        assert call_kwargs.kwargs["parse_mode"] == ParseMode.HTML
        markup = call_kwargs.kwargs["reply_markup"]
        assert markup is not None
        assert markup.inline_keyboard[0][0].text == "OPUS"

    async def test_suppresses_bad_request(self) -> None:
        from ductor_bot.orchestrator.selectors.models import SelectorResponse
        from ductor_bot.session.key import SessionKey

        tg_bot, bot_instance = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()
        key = SessionKey(chat_id=1)

        bot_instance.edit_message_text = AsyncMock(
            side_effect=TelegramBadRequest(method=MagicMock(), message="msg not modified")
        )

        resp = SelectorResponse(text="Pick:")
        with patch(
            "ductor_bot.orchestrator.selectors.model_selector.handle_model_callback",
            new_callable=AsyncMock,
            return_value=resp,
        ):
            await tg_bot._handle_model_selector(key, message_id=50, data="ms:p:claude")

        # Should not raise


class TestHandleCronSelector:
    async def test_edits_message_in_place(self) -> None:
        from ductor_bot.orchestrator.selectors.models import (
            Button,
            ButtonGrid,
            SelectorResponse,
        )

        tg_bot, bot_instance = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()
        grid = ButtonGrid(rows=[[Button(text="Refresh", callback_data="crn:r:0")]])
        resp = SelectorResponse(text="Cron panel", buttons=grid)

        with patch(
            "ductor_bot.orchestrator.selectors.cron_selector.handle_cron_callback",
            new_callable=AsyncMock,
            return_value=resp,
        ):
            await tg_bot._handle_cron_selector(chat_id=1, message_id=60, data="crn:r:0")

        from aiogram.enums import ParseMode

        call_kwargs = bot_instance.edit_message_text.call_args
        assert call_kwargs is not None
        assert call_kwargs.kwargs["text"] == "Cron panel"
        assert call_kwargs.kwargs["chat_id"] == 1
        assert call_kwargs.kwargs["message_id"] == 60
        assert call_kwargs.kwargs["parse_mode"] == ParseMode.HTML
        markup = call_kwargs.kwargs["reply_markup"]
        assert markup is not None
        assert markup.inline_keyboard[0][0].text == "Refresh"

    async def test_suppresses_bad_request(self) -> None:
        from ductor_bot.orchestrator.selectors.models import SelectorResponse

        tg_bot, bot_instance = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()

        bot_instance.edit_message_text = AsyncMock(
            side_effect=TelegramBadRequest(method=MagicMock(), message="msg not modified")
        )

        resp = SelectorResponse(text="Cron panel")
        with patch(
            "ductor_bot.orchestrator.selectors.cron_selector.handle_cron_callback",
            new_callable=AsyncMock,
            return_value=resp,
        ):
            await tg_bot._handle_cron_selector(chat_id=1, message_id=60, data="crn:r:0")

        # Should not raise


# ---------------------------------------------------------------------------
# _on_stop / _on_command / _on_new / _on_abort / _on_quick_command
# ---------------------------------------------------------------------------


class TestCommandHandlers:
    @patch(
        "ductor_bot.messenger.telegram.app.handle_abort", new_callable=AsyncMock, return_value=True
    )
    async def test_on_stop_calls_handle_abort(self, mock_abort: AsyncMock) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()
        msg = _make_message(chat_id=5)

        await tg_bot._on_stop(msg)

        mock_abort.assert_called_once()
        assert mock_abort.call_args.kwargs["chat_id"] == 5

    @patch("ductor_bot.messenger.telegram.app.handle_command", new_callable=AsyncMock)
    async def test_on_command_calls_handle_command(self, mock_cmd: AsyncMock) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch
        msg = _make_message(text="/status")

        await tg_bot._on_command(msg)

        mock_cmd.assert_called_once_with(orch, tg_bot.bot_instance, msg)

    @patch("ductor_bot.messenger.telegram.app.handle_new_session", new_callable=AsyncMock)
    async def test_on_new_calls_handle_new_session(self, mock_new: AsyncMock) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch
        msg = _make_message()

        await tg_bot._on_new(msg)

        mock_new.assert_called_once_with(
            orch, tg_bot.bot_instance, msg, topic_names=tg_bot._topic_names
        )

    @patch(
        "ductor_bot.messenger.telegram.app.handle_abort", new_callable=AsyncMock, return_value=True
    )
    async def test_on_abort_returns_handled(self, mock_abort: AsyncMock) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()
        msg = _make_message(chat_id=9)

        result = await tg_bot._on_abort(9, msg)

        assert result is True
        mock_abort.assert_called_once()

    @patch("ductor_bot.messenger.telegram.app.handle_command", new_callable=AsyncMock)
    async def test_on_quick_command_delegates(self, mock_cmd: AsyncMock) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch
        msg = _make_message()

        result = await tg_bot._on_quick_command(1, msg)

        assert result is True
        mock_cmd.assert_called_once()

    async def test_on_quick_command_returns_false_without_orchestrator(self) -> None:
        tg_bot, _ = _make_tg_bot()
        msg = _make_message()

        result = await tg_bot._on_quick_command(1, msg)

        assert result is False


# ---------------------------------------------------------------------------
# _handle_webhook_wake
# ---------------------------------------------------------------------------


class TestWebhookWake:
    async def test_calls_handle_message_and_sends_result(self) -> None:
        import ductor_bot.messenger.telegram.transport as _tgt

        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator(handle_message_text="Webhook reply")
        tg_bot._orchestrator = orch

        with patch.object(_tgt, "send_rich", new_callable=AsyncMock) as mock_send:
            result = await tg_bot._handle_webhook_wake(1, "Wake prompt")

        from ductor_bot.session.key import SessionKey

        orch.handle_message.assert_called_once_with(SessionKey(chat_id=1), "Wake prompt")
        mock_send.assert_called_once()
        assert mock_send.call_args[0][2] == "Webhook reply"
        assert result == "Webhook reply"

    async def test_acquires_per_chat_lock(self) -> None:
        import ductor_bot.messenger.telegram.transport as _tgt

        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        lock = tg_bot.sequential.get_lock(1)
        lock_was_held = False

        original_handle = orch.handle_message

        async def check_lock(*args: object, **kwargs: object) -> object:
            nonlocal lock_was_held
            lock_was_held = lock.locked()
            return await original_handle(*args, **kwargs)

        orch.handle_message = AsyncMock(side_effect=check_lock)

        with patch.object(_tgt, "send_rich", new_callable=AsyncMock):
            await tg_bot._handle_webhook_wake(1, "test")
        assert lock_was_held

    async def test_queues_behind_active_message(self) -> None:
        """Webhook wake waits for active conversation turn to finish."""
        import ductor_bot.messenger.telegram.transport as _tgt

        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch

        order: list[str] = []
        lock = tg_bot.sequential.get_lock(1)

        with patch.object(_tgt, "send_rich", new_callable=AsyncMock):
            async with lock:

                async def slow_handle(*_a: object, **_k: object) -> MagicMock:
                    order.append("webhook")
                    return MagicMock(text="ok")

                orch.handle_message = AsyncMock(side_effect=slow_handle)
                task = asyncio.create_task(tg_bot._handle_webhook_wake(1, "test"))

                await asyncio.sleep(0.01)
                order.append("user_done")

            await task
        assert order == ["user_done", "webhook"]


# ---------------------------------------------------------------------------
# _watch_restart_marker
# ---------------------------------------------------------------------------


class TestWatchRestartMarker:
    async def test_detects_marker_and_stops(self, tmp_path: Path) -> None:
        from ductor_bot.infra.restart import EXIT_RESTART

        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        orch.paths.ductor_home = tmp_path
        tg_bot._orchestrator = orch
        # stop_polling raises CancelledError to break the while-True loop,
        # matching production behavior where shutdown cancels the task.
        tg_bot._dp.stop_polling = AsyncMock(side_effect=asyncio.CancelledError)

        marker = tmp_path / "restart-requested"
        marker.write_text("1")

        # Patch both sleep (to skip 2s poll) and to_thread (to run synchronously)
        with (
            patch.object(asyncio, "sleep", new_callable=AsyncMock),
            patch.object(asyncio, "to_thread", new_callable=AsyncMock) as mock_to_thread,
        ):
            mock_to_thread.return_value = True  # consume_restart_marker returns True
            await tg_bot._watch_restart_marker()

        assert tg_bot._exit_code == EXIT_RESTART
        tg_bot._dp.stop_polling.assert_called_once()

    async def test_handles_cancellation(self) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator()
        tg_bot._orchestrator = orch
        tg_bot._dp.stop_polling = AsyncMock()

        with patch.object(
            asyncio, "sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError
        ):
            await tg_bot._watch_restart_marker()

        # CancelledError is caught internally, exit code stays 0
        assert tg_bot._exit_code == 0


# ---------------------------------------------------------------------------
# _sync_commands
# ---------------------------------------------------------------------------


class TestSyncCommands:
    async def test_sets_commands_when_different(self) -> None:
        from ductor_bot.messenger.telegram.app import _BOT_COMMANDS

        tg_bot, bot_instance = _make_tg_bot()
        bot_instance.get_my_commands = AsyncMock(return_value=[])
        bot_instance.set_my_commands = AsyncMock()
        bot_instance.delete_my_commands = AsyncMock()

        await tg_bot._sync_commands()

        bot_instance.set_my_commands.assert_called_once_with(_BOT_COMMANDS)

    async def test_skips_when_commands_match(self) -> None:
        from ductor_bot.messenger.telegram.app import _BOT_COMMANDS

        tg_bot, bot_instance = _make_tg_bot()
        desired = list(_BOT_COMMANDS)
        bot_instance.get_my_commands = AsyncMock(return_value=desired)
        bot_instance.set_my_commands = AsyncMock()
        bot_instance.delete_my_commands = AsyncMock()

        await tg_bot._sync_commands()

        bot_instance.set_my_commands.assert_not_called()

    async def test_clears_legacy_scoped_commands(self) -> None:
        """Old scoped commands (private/group) are deleted on sync."""
        from aiogram.types import BotCommand

        from ductor_bot.messenger.telegram.app import _BOT_COMMANDS

        tg_bot, bot_instance = _make_tg_bot()
        legacy = [BotCommand(command="old", description="legacy")]

        async def _get_my_commands(**kwargs: object) -> list[BotCommand]:
            if kwargs.get("scope"):
                return legacy
            return list(_BOT_COMMANDS)

        bot_instance.get_my_commands = AsyncMock(side_effect=_get_my_commands)
        bot_instance.set_my_commands = AsyncMock()
        bot_instance.delete_my_commands = AsyncMock()

        await tg_bot._sync_commands()

        assert bot_instance.delete_my_commands.call_count == 2
        bot_instance.set_my_commands.assert_not_called()

    async def test_updates_when_order_changes(self) -> None:
        """Reordering commands triggers an update (not just content diff)."""
        from ductor_bot.messenger.telegram.app import _BOT_COMMANDS

        tg_bot, bot_instance = _make_tg_bot()
        reversed_cmds = list(reversed(_BOT_COMMANDS))
        bot_instance.get_my_commands = AsyncMock(return_value=reversed_cmds)
        bot_instance.set_my_commands = AsyncMock()
        bot_instance.delete_my_commands = AsyncMock()

        await tg_bot._sync_commands()

        bot_instance.set_my_commands.assert_called_once_with(_BOT_COMMANDS)


# ---------------------------------------------------------------------------
# _file_roots
# ---------------------------------------------------------------------------


class TestFileRoots:
    def test_all_mode_returns_none(self) -> None:
        config = _make_config()
        config.file_access = "all"
        tg_bot, _ = _make_tg_bot(config)
        tg_bot._orchestrator = _make_orchestrator()
        assert tg_bot.file_roots(tg_bot._orch.paths) is None

    def test_home_mode_returns_home_dir(self) -> None:
        config = _make_config()
        config.file_access = "home"
        tg_bot, _ = _make_tg_bot(config)
        tg_bot._orchestrator = _make_orchestrator()
        roots = tg_bot.file_roots(tg_bot._orch.paths)
        assert roots == [Path.home()]

    def test_workspace_mode_returns_workspace(self) -> None:
        config = _make_config()
        config.file_access = "workspace"
        tg_bot, _ = _make_tg_bot(config)
        tg_bot._orchestrator = _make_orchestrator()
        roots = tg_bot.file_roots(tg_bot._orch.paths)
        assert roots == [tg_bot._orch.paths.workspace]

    def test_unknown_mode_falls_back_to_workspace(self) -> None:
        config = _make_config()
        config.file_access = "something_invalid"
        tg_bot, _ = _make_tg_bot(config)
        tg_bot._orchestrator = _make_orchestrator()
        roots = tg_bot.file_roots(tg_bot._orch.paths)
        assert roots == [tg_bot._orch.paths.workspace]

    def test_default_config_is_all(self) -> None:
        config = AgentConfig()
        assert config.file_access == "all"


# ---------------------------------------------------------------------------
# Forum topic propagation
# ---------------------------------------------------------------------------


class TestForumTopicPropagation:
    """Verify thread_id flows through _handle_streaming and _on_callback_query."""

    async def test_handle_streaming_passes_thread_id(self) -> None:
        tg_bot, _ = _make_tg_bot()
        orch = _make_orchestrator(handle_streaming_text="Fallback", stream_fallback=True)
        tg_bot._orchestrator = orch

        msg = _make_message(topic_thread_id=88)

        with patch(
            "ductor_bot.messenger.telegram.app.run_streaming_message", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = "Fallback"
            await tg_bot._handle_streaming(msg, 1, "test", thread_id=88)

        dispatch = mock_run.call_args.args[0]
        assert dispatch.thread_id == 88

    async def test_callback_query_passes_thread_id(self) -> None:
        config = _make_config(streaming_enabled=False)
        tg_bot, _ = _make_tg_bot(config)
        orch = _make_orchestrator(handle_message_text="Reply")
        tg_bot._orchestrator = orch

        cb = _make_callback_query(data="Approve", topic_thread_id=77)

        with patch(
            "ductor_bot.messenger.telegram.app.run_non_streaming_message", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = "Reply"
            await tg_bot._on_callback_query(cb)

        dispatch = mock_run.call_args.args[0]
        assert dispatch.thread_id == 77

    async def test_on_message_non_streaming_passes_thread_id(self) -> None:
        config = _make_config(streaming_enabled=False)
        tg_bot, _ = _make_tg_bot(config)
        orch = _make_orchestrator(handle_message_text="Reply")
        tg_bot._orchestrator = orch

        msg = _make_message(text="Hello", topic_thread_id=55)

        with patch(
            "ductor_bot.messenger.telegram.app.run_non_streaming_message", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = "Reply"
            await tg_bot._on_message(msg)

        dispatch = mock_run.call_args.args[0]
        assert dispatch.thread_id == 55

    @patch("ductor_bot.messenger.telegram.app.send_rich", new_callable=AsyncMock)
    async def test_on_help_passes_thread_id(self, mock_send: AsyncMock) -> None:
        tg_bot, _ = _make_tg_bot()
        msg = _make_message(topic_thread_id=33)

        await tg_bot._on_help(msg)

        opts = mock_send.call_args[0][3]
        assert opts.thread_id == 33

    @patch("ductor_bot.messenger.telegram.app.send_rich", new_callable=AsyncMock)
    async def test_on_restart_passes_thread_id(self, mock_send: AsyncMock) -> None:
        tg_bot, _ = _make_tg_bot()
        tg_bot._orchestrator = _make_orchestrator()
        tg_bot._dp.stop_polling = AsyncMock()
        msg = _make_message(topic_thread_id=44)

        with patch("ductor_bot.infra.restart.write_restart_sentinel"):
            await tg_bot._on_restart(msg)

        opts = mock_send.call_args[0][3]
        assert opts.thread_id == 44


class TestNotificationService:
    """TelegramNotificationService fan-out resilience."""

    async def test_notify_all_skips_unreachable_recipient(self) -> None:
        """A 'chat not found' recipient (never pressed /start) must not abort
        the fan-out — at startup it used to crash the whole boot."""
        from ductor_bot.messenger.telegram.app import TelegramNotificationService

        config = _make_config(user_ids=[1, 2])
        service = TelegramNotificationService(MagicMock(), config)
        sent: list[int] = []

        async def fake_send_rich(_bot: object, uid: int, _text: str, _opts: object) -> bool:
            if uid == 1:
                raise TelegramBadRequest(method=MagicMock(), message="chat not found")
            sent.append(uid)
            return True

        with patch("ductor_bot.messenger.telegram.app.send_rich", side_effect=fake_send_rich):
            await service.notify_all("hello")

        assert sent == [2]
