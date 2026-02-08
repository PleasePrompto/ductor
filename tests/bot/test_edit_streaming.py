"""Tests for EditStreamEditor (edit-mode: single message, in-place edits)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import Message

if TYPE_CHECKING:
    from ductor_bot.bot.edit_streaming import EditStreamEditor


def _make_editor(
    *,
    reply_to: Message | None = None,
    edit_interval: float = 0.0,
    max_failures: int = 3,
) -> tuple[MagicMock, EditStreamEditor]:
    """Create a bot mock and an EditStreamEditor with zero throttle by default."""
    from ductor_bot.bot.edit_streaming import EditStreamEditor

    bot = MagicMock()
    sent_msg = MagicMock(spec=Message)
    type(sent_msg).message_id = PropertyMock(return_value=42)
    bot.send_message = AsyncMock(return_value=sent_msg)
    bot.edit_message_text = AsyncMock()
    if reply_to is not None:
        object.__setattr__(reply_to, "answer", AsyncMock(return_value=sent_msg))

    editor = EditStreamEditor(
        bot,
        chat_id=1,
        reply_to=reply_to,
        edit_interval_seconds=edit_interval,
        max_edit_failures=max_failures,
    )
    return bot, editor


class TestEditStreamEditor:
    """Test edit-mode streaming: single message with in-place edits."""

    async def test_has_content_initially_false(self) -> None:
        _, editor = _make_editor()
        assert editor.has_content is False

    async def test_first_text_creates_message(self) -> None:
        bot, editor = _make_editor()
        await editor.append_text("Hello world")
        await editor.finalize("")
        assert editor.has_content is True
        bot.send_message.assert_called_once()
        assert bot.send_message.call_args.kwargs["parse_mode"] == ParseMode.HTML

    async def test_first_text_replies_when_reply_to_set(self) -> None:
        reply_msg = MagicMock(spec=Message)
        _, editor = _make_editor(reply_to=reply_msg)
        await editor.append_text("First")
        await editor.finalize("")
        reply_msg.answer.assert_called_once()

    async def test_second_text_edits_same_message(self) -> None:
        bot, editor = _make_editor()
        await editor.append_text("First chunk")
        await editor.append_text("Second chunk")
        await editor.finalize("")
        # First text creates, subsequent edit
        bot.edit_message_text.assert_called()
        assert "First chunk" in bot.edit_message_text.call_args.kwargs["text"] or (
            "Second chunk" in bot.edit_message_text.call_args.kwargs["text"]
        )

    async def test_tool_collapse_same_name(self) -> None:
        bot, editor = _make_editor()
        await editor.append_tool("Bash")
        await editor.append_tool("Bash")
        await editor.append_tool("Bash")
        await editor.finalize("")
        # Find the last call that contains the tool indicator
        last_text = self._get_last_message_text(bot)
        assert "[TOOL: Bash] x3" in last_text

    async def test_tool_collapse_mixed(self) -> None:
        bot, editor = _make_editor()
        await editor.append_tool("Bash")
        await editor.append_tool("Bash")
        await editor.append_tool("Write")
        await editor.finalize("")
        last_text = self._get_last_message_text(bot)
        assert "[TOOL: Bash] x2" in last_text
        assert "[TOOL: Write]" in last_text
        assert "x1" not in last_text  # Single tools have no count

    async def test_text_tool_text_ordering(self) -> None:
        bot, editor = _make_editor()
        await editor.append_text("Before tools")
        await editor.append_tool("Bash")
        await editor.append_text("After tools")
        await editor.finalize("")
        last_text = self._get_last_message_text(bot)
        # Indicators are stripped from the final message
        assert "Before tools" in last_text
        assert "After tools" in last_text
        assert "[TOOL: Bash]" not in last_text
        # Text ordering is preserved
        before_pos = last_text.find("Before tools")
        after_pos = last_text.find("After tools")
        assert before_pos < after_pos

    async def test_empty_text_ignored(self) -> None:
        bot, editor = _make_editor()
        await editor.append_text("")
        await editor.append_text("   ")
        await editor.finalize("")
        bot.send_message.assert_not_called()
        bot.edit_message_text.assert_not_called()
        assert editor.has_content is False

    async def test_finalize_forces_edit(self) -> None:
        _bot, editor = _make_editor()
        await editor.append_text("Content")
        await editor.finalize("")
        assert editor.has_content is True

    async def test_message_not_modified_ignored(self) -> None:
        bot, editor = _make_editor()
        await editor.append_text("Content")
        await editor.finalize("")

        # Now edit with same content triggers "not modified"
        bot.edit_message_text = AsyncMock(
            side_effect=TelegramBadRequest(MagicMock(), "message is not modified"),
        )
        await editor.append_text("More content")
        await editor.finalize("")
        # Should not raise, should not fall back

    async def test_edit_failure_fallback(self) -> None:
        bot, editor = _make_editor(max_failures=2)
        await editor.append_text("Initial")
        await editor.finalize("")

        # Make edits fail with non-"not modified" errors
        bot.edit_message_text = AsyncMock(
            side_effect=TelegramBadRequest(MagicMock(), "bad request"),
        )
        await editor.append_text("Fail 1")
        await editor.finalize("")
        await editor.append_text("Fail 2")
        await editor.finalize("")

        # After max_failures, should fall back to append (send_message)
        send_count_before = bot.send_message.call_count
        await editor.append_text("Appended")
        await editor.finalize("")
        assert bot.send_message.call_count > send_count_before

    async def test_rate_limit_retry(self) -> None:
        bot, editor = _make_editor()
        await editor.append_text("Initial")
        await editor.finalize("")

        retry_exc = TelegramRetryAfter(MagicMock(), "retry after", retry_after=0)
        # retry_exc on first edit, success on retry, success on finalize's edit
        bot.edit_message_text = AsyncMock(
            side_effect=[retry_exc, None, None],
        )
        await editor.append_text("Retry content")
        await editor.finalize("")
        assert bot.edit_message_text.call_count >= 2

    async def test_has_content_after_tool(self) -> None:
        _, editor = _make_editor()
        await editor.append_tool("Bash")
        await editor.finalize("")
        assert editor.has_content is True

    async def test_single_tool_no_count_suffix(self) -> None:
        bot, editor = _make_editor()
        await editor.append_tool("Read")
        await editor.finalize("")
        last_text = self._get_last_message_text(bot)
        assert "[TOOL: Read]" in last_text
        assert "x1" not in last_text

    @staticmethod
    def _get_last_message_text(bot: MagicMock) -> str:
        """Extract the text from the last send_message or edit_message_text call."""
        if bot.edit_message_text.call_count > 0:
            return str(bot.edit_message_text.call_args.kwargs.get("text", ""))
        if bot.send_message.call_count > 0:
            return str(bot.send_message.call_args.kwargs.get("text", ""))
        return ""


class TestEditStreamEditorButtons:
    """Test button keyboard attachment at finalize time."""

    async def test_finalize_with_buttons_calls_edit_reply_markup(self) -> None:
        bot, editor = _make_editor()
        bot.edit_message_reply_markup = AsyncMock()
        await editor.append_text("Choose:\n\n[button:Yes] [button:No]")
        await editor.finalize("Choose:\n\n[button:Yes] [button:No]")
        bot.edit_message_reply_markup.assert_called_once()
        call_kwargs = bot.edit_message_reply_markup.call_args.kwargs
        assert call_kwargs["chat_id"] == 1
        assert call_kwargs["message_id"] == 42
        markup = call_kwargs["reply_markup"]
        assert len(markup.inline_keyboard) == 1
        assert markup.inline_keyboard[0][0].text == "Yes"
        assert markup.inline_keyboard[0][1].text == "No"

    async def test_finalize_without_buttons_no_reply_markup_call(self) -> None:
        bot, editor = _make_editor()
        bot.edit_message_reply_markup = AsyncMock()
        await editor.append_text("Plain text, no buttons.")
        await editor.finalize("Plain text, no buttons.")
        bot.edit_message_reply_markup.assert_not_called()

    async def test_finalize_buttons_extracted_from_full_text(self) -> None:
        """Buttons are parsed from the full_text param, not segments."""
        bot, editor = _make_editor()
        bot.edit_message_reply_markup = AsyncMock()
        # Streaming text has no buttons, but full_text does
        await editor.append_text("Content here.")
        await editor.finalize("Content here.\n\n[button:Action]")
        bot.edit_message_reply_markup.assert_called_once()
        markup = bot.edit_message_reply_markup.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].text == "Action"

    async def test_finalize_buttons_with_multiple_rows(self) -> None:
        bot, editor = _make_editor()
        bot.edit_message_reply_markup = AsyncMock()
        full = "Menu:\n\n[button:A] [button:B]\n[button:Cancel]"
        await editor.append_text("Menu:")
        await editor.finalize(full)
        markup = bot.edit_message_reply_markup.call_args.kwargs["reply_markup"]
        assert len(markup.inline_keyboard) == 2
        assert len(markup.inline_keyboard[0]) == 2
        assert len(markup.inline_keyboard[1]) == 1

    async def test_finalize_buttons_not_called_when_fallen_back(self) -> None:
        bot, editor = _make_editor(max_failures=1)
        bot.edit_message_reply_markup = AsyncMock()
        await editor.append_text("Initial")
        await editor.finalize("")
        # Force fallback
        bot.edit_message_text = AsyncMock(
            side_effect=TelegramBadRequest(MagicMock(), "bad request"),
        )
        await editor.append_text("Fail")
        await editor.finalize("")
        # Now in fallback mode
        await editor.finalize("[button:Nope]")
        bot.edit_message_reply_markup.assert_not_called()

    async def test_finalize_buttons_not_called_when_no_active_msg(self) -> None:
        bot, editor = _make_editor()
        bot.edit_message_reply_markup = AsyncMock()
        # Never sent any text, so no active_msg
        await editor.finalize("[button:Ghost]")
        bot.edit_message_reply_markup.assert_not_called()


class TestToolTracker:
    """Test the internal _ToolTracker collapsing logic."""

    def test_single_tool(self) -> None:
        from ductor_bot.bot.edit_streaming import _ToolTracker

        tracker = _ToolTracker()
        tracker.add("Bash")
        result = tracker.render_html()
        assert "[TOOL: Bash]</b>" in result
        assert "x" not in result

    def test_consecutive_same(self) -> None:
        from ductor_bot.bot.edit_streaming import _ToolTracker

        tracker = _ToolTracker()
        for _ in range(4):
            tracker.add("Bash")
        result = tracker.render_html()
        assert "[TOOL: Bash] x4</b>" in result

    def test_mixed_tools(self) -> None:
        from ductor_bot.bot.edit_streaming import _ToolTracker

        tracker = _ToolTracker()
        tracker.add("Bash")
        tracker.add("Bash")
        tracker.add("Write")
        tracker.add("Read")
        tracker.add("Read")
        tracker.add("Read")
        result = tracker.render_html()
        assert "[TOOL: Bash] x2" in result
        assert "[TOOL: Write]" in result
        assert "[TOOL: Read] x3" in result

    def test_empty_tracker(self) -> None:
        from ductor_bot.bot.edit_streaming import _ToolTracker

        tracker = _ToolTracker()
        assert not tracker.has_entries
        assert tracker.render_html() == ""

    def test_html_escaping(self) -> None:
        from ductor_bot.bot.edit_streaming import _ToolTracker

        tracker = _ToolTracker()
        tracker.add("<script>")
        result = tracker.render_html()
        assert "&lt;script&gt;" in result
        assert "<script>" not in result

    def test_system_style_collapsing(self) -> None:
        from ductor_bot.bot.edit_streaming import _ToolTracker

        tracker = _ToolTracker()
        tracker.add("THINKING", style="system")
        tracker.add("THINKING", style="system")
        tracker.add("THINKING", style="system")
        result = tracker.render_html()
        assert "[THINKING] x3" in result
        assert "<i>" in result

    def test_mixed_tool_and_system(self) -> None:
        from ductor_bot.bot.edit_streaming import _ToolTracker

        tracker = _ToolTracker()
        tracker.add("THINKING", style="system")
        tracker.add("Bash", style="tool")
        tracker.add("THINKING", style="system")
        result = tracker.render_html()
        assert "<i>[THINKING]</i>" in result
        assert "<b>[TOOL: Bash]</b>" in result
        # Two separate THINKING entries (not collapsed across tool)
        assert "x" not in result

    def test_system_not_collapsed_with_tool(self) -> None:
        from ductor_bot.bot.edit_streaming import _ToolTracker

        tracker = _ToolTracker()
        tracker.add("THINKING", style="system")
        tracker.add("THINKING", style="tool")
        result = tracker.render_html()
        assert "<i>[THINKING]</i>" in result
        assert "<b>[TOOL: THINKING]</b>" in result


class TestIndicatorStripping:
    """Test that finalize strips all indicators from the final message."""

    async def test_finalize_strips_tool_indicators(self) -> None:
        bot, editor = _make_editor()
        await editor.append_text("Hello")
        await editor.append_tool("Bash")
        await editor.append_tool("Bash")
        await editor.append_text("World")
        await editor.finalize("")
        last_text = _get_last_text(bot)
        assert "Hello" in last_text
        assert "World" in last_text
        assert "TOOL" not in last_text

    async def test_finalize_strips_system_indicators(self) -> None:
        bot, editor = _make_editor()
        await editor.append_text("Start")
        await editor.append_system("THINKING")
        await editor.append_text("End")
        await editor.finalize("")
        last_text = _get_last_text(bot)
        assert "Start" in last_text
        assert "End" in last_text
        assert "THINKING" not in last_text

    async def test_finalize_strips_mixed_indicators(self) -> None:
        bot, editor = _make_editor()
        await editor.append_text("A")
        await editor.append_system("THINKING")
        await editor.append_tool("Bash")
        await editor.append_system("THINKING")
        await editor.append_text("B")
        await editor.finalize("")
        last_text = _get_last_text(bot)
        assert "THINKING" not in last_text
        assert "TOOL" not in last_text


def _get_last_text(bot: MagicMock) -> str:
    if bot.edit_message_text.call_count > 0:
        return str(bot.edit_message_text.call_args.kwargs.get("text", ""))
    if bot.send_message.call_count > 0:
        return str(bot.send_message.call_args.kwargs.get("text", ""))
    return ""
