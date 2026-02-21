"""Tests for the stream editor factory function."""

from __future__ import annotations

from unittest.mock import MagicMock

from ductor_bot.config import StreamingConfig


class TestCreateStreamEditor:
    """Verify factory returns the correct editor type based on append_mode."""

    def test_append_mode_returns_stream_editor(self) -> None:
        from ductor_bot.bot.streaming import StreamEditor, create_stream_editor

        bot = MagicMock()
        cfg = StreamingConfig(append_mode=True)
        editor = create_stream_editor(bot, chat_id=1, cfg=cfg)
        assert isinstance(editor, StreamEditor)

    def test_edit_mode_returns_edit_stream_editor(self) -> None:
        from ductor_bot.bot.edit_streaming import EditStreamEditor
        from ductor_bot.bot.streaming import create_stream_editor

        bot = MagicMock()
        cfg = StreamingConfig(append_mode=False)
        editor = create_stream_editor(bot, chat_id=1, cfg=cfg)
        assert isinstance(editor, EditStreamEditor)

    def test_thread_id_passed_to_stream_editor(self) -> None:
        from ductor_bot.bot.streaming import StreamEditor, create_stream_editor

        bot = MagicMock()
        cfg = StreamingConfig(append_mode=True)
        editor = create_stream_editor(bot, chat_id=1, cfg=cfg, thread_id=42)
        assert isinstance(editor, StreamEditor)
        assert editor._thread_id == 42

    def test_thread_id_passed_to_edit_stream_editor(self) -> None:
        from ductor_bot.bot.edit_streaming import EditStreamEditor
        from ductor_bot.bot.streaming import create_stream_editor

        bot = MagicMock()
        cfg = StreamingConfig(append_mode=False)
        editor = create_stream_editor(bot, chat_id=1, cfg=cfg, thread_id=42)
        assert isinstance(editor, EditStreamEditor)
        assert editor._thread_id == 42
