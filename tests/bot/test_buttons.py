"""Tests for button extraction from CLI output text."""

from __future__ import annotations


class TestExtractButtons:
    """Test [button:...] pattern parsing and InlineKeyboardMarkup generation."""

    def test_single_button_extracted(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        clean, markup = extract_buttons("Pick one:\n\n[button:Yes]")
        assert "Pick one:" in clean
        assert "[button:" not in clean
        assert markup is not None
        assert len(markup.inline_keyboard) == 1
        assert len(markup.inline_keyboard[0]) == 1
        assert markup.inline_keyboard[0][0].text == "Yes"
        assert markup.inline_keyboard[0][0].callback_data == "Yes"

    def test_two_buttons_same_line_one_row(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        _clean, markup = extract_buttons("Choose:\n\n[button:Yes] [button:No]")
        assert markup is not None
        assert len(markup.inline_keyboard) == 1
        row = markup.inline_keyboard[0]
        assert len(row) == 2
        assert row[0].text == "Yes"
        assert row[1].text == "No"

    def test_buttons_on_separate_lines_separate_rows(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        text = "Menu:\n\n[button:Option A]\n[button:Option B]\n[button:Cancel]"
        _clean, markup = extract_buttons(text)
        assert markup is not None
        assert len(markup.inline_keyboard) == 3
        assert markup.inline_keyboard[0][0].text == "Option A"
        assert markup.inline_keyboard[1][0].text == "Option B"
        assert markup.inline_keyboard[2][0].text == "Cancel"

    def test_mixed_rows_and_columns(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        text = "[button:A] [button:B]\n[button:C]"
        _clean, markup = extract_buttons(text)
        assert markup is not None
        assert len(markup.inline_keyboard) == 2
        assert len(markup.inline_keyboard[0]) == 2
        assert len(markup.inline_keyboard[1]) == 1
        assert markup.inline_keyboard[0][0].text == "A"
        assert markup.inline_keyboard[0][1].text == "B"
        assert markup.inline_keyboard[1][0].text == "C"

    def test_no_buttons_returns_none_markup(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        clean, markup = extract_buttons("Just a regular message with no buttons.")
        assert markup is None
        assert clean == "Just a regular message with no buttons."

    def test_empty_string(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        clean, markup = extract_buttons("")
        assert clean == ""
        assert markup is None

    def test_text_preserved_around_buttons(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        text = "Header text here.\n\nSome body.\n\n[button:Click me]"
        clean, markup = extract_buttons(text)
        assert "Header text here." in clean
        assert "Some body." in clean
        assert "[button:" not in clean
        assert markup is not None

    def test_only_buttons_no_text(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        text = "[button:Alpha] [button:Beta]"
        clean, markup = extract_buttons(text)
        assert clean.strip() == ""
        assert markup is not None
        assert len(markup.inline_keyboard[0]) == 2

    def test_button_inside_code_block_not_extracted(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        text = "Example:\n```\n[button:NotAButton]\n```\n\n[button:RealButton]"
        clean, markup = extract_buttons(text)
        assert markup is not None
        # Only RealButton should be extracted
        all_buttons = [btn for row in markup.inline_keyboard for btn in row]
        assert len(all_buttons) == 1
        assert all_buttons[0].text == "RealButton"
        # Code block content preserved in clean text
        assert "[button:NotAButton]" in clean

    def test_button_inside_inline_code_not_extracted(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        text = "Use `[button:fake]` syntax.\n\n[button:Real]"
        clean, markup = extract_buttons(text)
        assert markup is not None
        all_buttons = [btn for row in markup.inline_keyboard for btn in row]
        assert len(all_buttons) == 1
        assert all_buttons[0].text == "Real"
        assert "`[button:fake]`" in clean

    def test_callback_data_truncated_at_64_bytes(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        long_text = "A" * 100
        text = f"[button:{long_text}]"
        _clean, markup = extract_buttons(text)
        assert markup is not None
        btn = markup.inline_keyboard[0][0]
        assert btn.callback_data is not None
        assert len(btn.callback_data.encode("utf-8")) <= 64

    def test_callback_data_preserves_short_text(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        _, markup = extract_buttons("[button:Short]")
        assert markup is not None
        assert markup.inline_keyboard[0][0].callback_data == "Short"

    def test_special_characters_in_button_text(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        text = "[button:Yes! 👍] [button:No & cancel]"
        _clean, markup = extract_buttons(text)
        assert markup is not None
        assert markup.inline_keyboard[0][0].text == "Yes! 👍"
        assert markup.inline_keyboard[0][1].text == "No & cancel"

    def test_empty_button_text_skipped(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        text = "[button:] [button:Valid]"
        _clean, markup = extract_buttons(text)
        assert markup is not None
        all_buttons = [btn for row in markup.inline_keyboard for btn in row]
        assert len(all_buttons) == 1
        assert all_buttons[0].text == "Valid"

    def test_whitespace_only_button_text_skipped(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        text = "[button:   ] [button:Ok]"
        _, markup = extract_buttons(text)
        assert markup is not None
        all_buttons = [btn for row in markup.inline_keyboard for btn in row]
        assert len(all_buttons) == 1
        assert all_buttons[0].text == "Ok"

    def test_parentheses_in_button_text(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        text = "[button:Option (1)]"
        _, markup = extract_buttons(text)
        assert markup is not None
        assert markup.inline_keyboard[0][0].text == "Option (1)"

    def test_button_text_stripped(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        text = "[button:  Padded  ]"
        _, markup = extract_buttons(text)
        assert markup is not None
        assert markup.inline_keyboard[0][0].text == "Padded"

    def test_trailing_newlines_cleaned_from_output(self) -> None:
        from ductor_bot.bot.buttons import extract_buttons

        text = "Content\n\n[button:Go]\n\n"
        clean, _ = extract_buttons(text)
        assert not clean.endswith("\n\n\n")

    def test_buttons_between_text_paragraphs(self) -> None:
        """Buttons that appear between paragraphs are still extracted."""
        from ductor_bot.bot.buttons import extract_buttons

        text = "First paragraph.\n\n[button:Middle]\n\nSecond paragraph."
        clean, markup = extract_buttons(text)
        assert "First paragraph." in clean
        assert "Second paragraph." in clean
        assert "[button:" not in clean
        assert markup is not None
        assert markup.inline_keyboard[0][0].text == "Middle"

    def test_multiple_button_blocks_collected(self) -> None:
        """Buttons scattered across the text are all collected."""
        from ductor_bot.bot.buttons import extract_buttons

        text = "Text\n[button:A]\nMore text\n[button:B] [button:C]"
        _, markup = extract_buttons(text)
        assert markup is not None
        all_buttons = [btn for row in markup.inline_keyboard for btn in row]
        assert len(all_buttons) == 3
        texts = {b.text for b in all_buttons}
        assert texts == {"A", "B", "C"}


class TestStripButtonSyntax:
    """Test the strip function used by the formatting pipeline."""

    def test_strips_all_button_patterns(self) -> None:
        from ductor_bot.bot.buttons import strip_button_syntax

        text = "Hello\n\n[button:Yes] [button:No]"
        result = strip_button_syntax(text)
        assert "[button:" not in result
        assert "Hello" in result

    def test_preserves_code_block_buttons(self) -> None:
        from ductor_bot.bot.buttons import strip_button_syntax

        text = "```\n[button:InCode]\n```\n\n[button:Outside]"
        result = strip_button_syntax(text)
        assert "[button:InCode]" in result
        assert "[button:Outside]" not in result

    def test_preserves_inline_code_buttons(self) -> None:
        from ductor_bot.bot.buttons import strip_button_syntax

        text = "Try `[button:example]` syntax\n[button:Real]"
        result = strip_button_syntax(text)
        assert "`[button:example]`" in result
        assert "\n[button:Real]" not in result

    def test_no_buttons_unchanged(self) -> None:
        from ductor_bot.bot.buttons import strip_button_syntax

        text = "No buttons here at all."
        assert strip_button_syntax(text) == text

    def test_empty_lines_collapsed(self) -> None:
        from ductor_bot.bot.buttons import strip_button_syntax

        text = "Text\n\n[button:Go]\n\nMore"
        result = strip_button_syntax(text)
        # Should not leave triple+ newlines from removal
        assert "\n\n\n" not in result
