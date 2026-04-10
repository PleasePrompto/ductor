"""Tests for channel whitelist and auto-leave feature.

Covers:
- _on_bot_added: auto-leave non-whitelisted channels with the correct message
- _on_bot_added: allow whitelisted channels (no leave)
- _on_bot_added: group behaviour unchanged by channel whitelist
- audit_groups: whitelisted channels are not evicted
- audit_groups: non-whitelisted active channels are left
- Config: allowed_channel_ids field defaults to empty list
- Hot-reload: allowed_channel_ids updated in-place
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberUpdated

from ductor_bot.config import AgentConfig, StreamingConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    group_ids: list[int] | None = None,
    channel_ids: list[int] | None = None,
) -> AgentConfig:
    return AgentConfig(
        telegram_token="test:token",
        allowed_user_ids=[100],
        allowed_group_ids=group_ids or [],
        allowed_channel_ids=channel_ids or [],
        streaming=StreamingConfig(enabled=False),
    )


def _make_tg_bot(config: AgentConfig) -> tuple[MagicMock, MagicMock]:
    """Return (tg_bot, mock_bot_instance)."""
    from ductor_bot.messenger.telegram.app import TelegramBot

    bot_instance = MagicMock()
    bot_instance.send_message = AsyncMock()
    bot_instance.leave_chat = AsyncMock()
    bot_instance.edit_message_reply_markup = AsyncMock()
    bot_instance.edit_message_text = AsyncMock()
    bot_instance.send_photo = AsyncMock()
    bot_instance.send_chat_action = AsyncMock()
    bot_instance.delete_webhook = AsyncMock()

    with patch("ductor_bot.messenger.telegram.app.Bot", return_value=bot_instance):
        tg_bot = TelegramBot(config)

    return tg_bot, bot_instance


def _make_member_event(chat_id: int, chat_type: str, title: str = "Test Chat") -> MagicMock:
    """Create a mock ChatMemberUpdated event."""
    event = MagicMock(spec=ChatMemberUpdated)
    event.chat = MagicMock()
    event.chat.id = chat_id
    event.chat.type = chat_type
    event.chat.title = title
    event.new_chat_member = MagicMock()
    event.new_chat_member.status = "member"
    return event


# ---------------------------------------------------------------------------
# Config field
# ---------------------------------------------------------------------------


class TestAllowedChannelIdsConfig:
    def test_defaults_to_empty(self) -> None:
        cfg = AgentConfig(telegram_token="t:x", allowed_user_ids=[1])
        assert cfg.allowed_channel_ids == []

    def test_accepts_channel_ids(self) -> None:
        cfg = AgentConfig(
            telegram_token="t:x",
            allowed_user_ids=[1],
            allowed_channel_ids=[-1001234567890],
        )
        assert cfg.allowed_channel_ids == [-1001234567890]


# ---------------------------------------------------------------------------
# _on_bot_added — channel handling
# ---------------------------------------------------------------------------


class TestOnBotAddedChannel:
    async def test_non_whitelisted_channel_sends_message_and_leaves(self) -> None:
        cfg = _make_config(channel_ids=[])  # no channels whitelisted
        tg_bot, bot = _make_tg_bot(cfg)
        event = _make_member_event(chat_id=-1001111111111, chat_type="channel")

        await tg_bot._on_bot_added(event)

        bot.send_message.assert_awaited_once()
        call_args = bot.send_message.call_args
        assert call_args.args[0] == -1001111111111
        # Message must mention "channel" and "whitelisted"
        sent_text: str = call_args.args[1]
        assert "channel" in sent_text.lower()
        assert "whitelisted" in sent_text.lower()

        bot.leave_chat.assert_awaited_once_with(-1001111111111)

    async def test_non_whitelisted_channel_leave_message_exact(self) -> None:
        """The exact i18n string must be used."""
        from ductor_bot.i18n import t

        cfg = _make_config(channel_ids=[])
        tg_bot, bot = _make_tg_bot(cfg)
        event = _make_member_event(chat_id=-1001111111111, chat_type="channel")

        await tg_bot._on_bot_added(event)

        sent_text = bot.send_message.call_args.args[1]
        assert sent_text == t("telegram.channel_not_whitelisted")

    async def test_whitelisted_channel_does_not_leave(self) -> None:
        channel_id = -1001111111111
        cfg = _make_config(channel_ids=[channel_id])
        tg_bot, bot = _make_tg_bot(cfg)
        # _send_join_notification reads a file; patch it out
        tg_bot._send_join_notification = AsyncMock()
        event = _make_member_event(chat_id=channel_id, chat_type="channel")

        await tg_bot._on_bot_added(event)

        bot.send_message.assert_not_awaited()
        bot.leave_chat.assert_not_awaited()
        tg_bot._send_join_notification.assert_awaited_once_with(channel_id)

    async def test_leave_chat_api_error_is_suppressed(self) -> None:
        """TelegramAPIError during leave_chat must not propagate."""
        cfg = _make_config(channel_ids=[])
        tg_bot, bot = _make_tg_bot(cfg)
        bot.leave_chat.side_effect = TelegramAPIError(method=MagicMock(), message="forbidden")
        event = _make_member_event(chat_id=-1001111111111, chat_type="channel")

        # Should not raise
        await tg_bot._on_bot_added(event)

    async def test_send_message_api_error_is_suppressed(self) -> None:
        """TelegramAPIError during send_message must not propagate."""
        cfg = _make_config(channel_ids=[])
        tg_bot, bot = _make_tg_bot(cfg)
        bot.send_message.side_effect = TelegramAPIError(method=MagicMock(), message="forbidden")
        event = _make_member_event(chat_id=-1001111111111, chat_type="channel")

        await tg_bot._on_bot_added(event)

        # leave_chat is still called even when send_message fails
        bot.leave_chat.assert_awaited_once_with(-1001111111111)


# ---------------------------------------------------------------------------
# _on_bot_added — group behaviour unchanged
# ---------------------------------------------------------------------------


class TestOnBotAddedGroupUnchanged:
    async def test_non_whitelisted_group_still_uses_group_message(self) -> None:
        from ductor_bot.i18n import t

        cfg = _make_config(group_ids=[], channel_ids=[])
        tg_bot, bot = _make_tg_bot(cfg)
        event = _make_member_event(chat_id=-100222222222, chat_type="supergroup")

        await tg_bot._on_bot_added(event)

        sent_text = bot.send_message.call_args.args[1]
        assert sent_text == t("telegram.group_rejected")
        bot.leave_chat.assert_awaited_once_with(-100222222222)

    async def test_whitelisted_group_is_not_affected_by_channel_ids(self) -> None:
        group_id = -100222222222
        cfg = _make_config(group_ids=[group_id], channel_ids=[-1001111111111])
        tg_bot, bot = _make_tg_bot(cfg)
        tg_bot._send_join_notification = AsyncMock()
        event = _make_member_event(chat_id=group_id, chat_type="supergroup")

        await tg_bot._on_bot_added(event)

        bot.send_message.assert_not_awaited()
        bot.leave_chat.assert_not_awaited()


# ---------------------------------------------------------------------------
# audit_groups — channel awareness
# ---------------------------------------------------------------------------


class TestAuditGroupsChannelAwareness:
    def _make_chat_record(
        self,
        chat_id: int,
        chat_type: str,
        status: str = "active",
    ) -> MagicMock:
        rec = MagicMock()
        rec.chat_id = chat_id
        rec.chat_type = chat_type
        rec.status = status
        rec.title = "Test"
        return rec

    async def test_whitelisted_channel_is_not_left_during_audit(self) -> None:
        channel_id = -1001111111111
        cfg = _make_config(channel_ids=[channel_id])
        tg_bot, bot = _make_tg_bot(cfg)

        tracker = MagicMock()
        tracker.get_all.return_value = [
            self._make_chat_record(channel_id, "channel"),
        ]
        tracker.record_leave = MagicMock()
        tg_bot._chat_tracker = tracker

        left = await tg_bot.audit_groups()

        assert left == 0
        bot.leave_chat.assert_not_awaited()

    async def test_non_whitelisted_active_channel_is_left_during_audit(self) -> None:
        channel_id = -1001111111111
        cfg = _make_config(channel_ids=[])  # not whitelisted
        tg_bot, bot = _make_tg_bot(cfg)

        tracker = MagicMock()
        tracker.get_all.return_value = [
            self._make_chat_record(channel_id, "channel"),
        ]
        tracker.record_leave = MagicMock()
        tg_bot._chat_tracker = tracker

        left = await tg_bot.audit_groups()

        assert left == 1
        bot.leave_chat.assert_awaited_once_with(channel_id)
        tracker.record_leave.assert_called_once_with(channel_id, "auto_left")

    async def test_whitelisted_group_still_safe_during_audit(self) -> None:
        group_id = -100222222222
        cfg = _make_config(group_ids=[group_id])
        tg_bot, bot = _make_tg_bot(cfg)

        tracker = MagicMock()
        tracker.get_all.return_value = [
            self._make_chat_record(group_id, "supergroup"),
        ]
        tracker.record_leave = MagicMock()
        tg_bot._chat_tracker = tracker

        left = await tg_bot.audit_groups()

        assert left == 0
        bot.leave_chat.assert_not_awaited()


# ---------------------------------------------------------------------------
# Hot-reload
# ---------------------------------------------------------------------------


class TestHotReloadChannelIds:
    async def test_allowed_channel_ids_updated_on_hot_reload(self) -> None:
        channel_id = -1001111111111
        cfg = _make_config(channel_ids=[])
        tg_bot, _ = _make_tg_bot(cfg)

        assert channel_id not in tg_bot._allowed_channels

        new_cfg = _make_config(channel_ids=[channel_id])
        tg_bot._on_auth_hot_reload(new_cfg, {"allowed_channel_ids": [channel_id]})

        assert channel_id in tg_bot._allowed_channels

    async def test_unrelated_hot_reload_does_not_touch_channels(self) -> None:
        channel_id = -1001111111111
        cfg = _make_config(channel_ids=[channel_id])
        tg_bot, _ = _make_tg_bot(cfg)

        new_cfg = _make_config(channel_ids=[channel_id])
        # Only language changed — channels must be untouched
        tg_bot._on_auth_hot_reload(new_cfg, {"language": "de"})

        assert channel_id in tg_bot._allowed_channels
