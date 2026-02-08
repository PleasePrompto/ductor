"""Tests for markdown_to_telegram_html and split_html_message."""

from __future__ import annotations


class TestMarkdownToTelegramHTML:
    """Test Markdown -> Telegram HTML conversion."""

    def test_plain_text_is_html_escaped(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("Hello <world> & 'friends'")
        assert "&lt;world&gt;" in result
        assert "&amp;" in result

    def test_bold_text(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("This is **bold** text")
        assert "<b>bold</b>" in result

    def test_italic_text(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("This is *italic* text")
        assert "<i>italic</i>" in result

    def test_strikethrough_text(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("This is ~~deleted~~ text")
        assert "<s>deleted</s>" in result

    def test_inline_code(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("Use `print()` here")
        assert "<code>print()</code>" in result

    def test_inline_code_html_escaped(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("Use `a < b` here")
        assert "<code>a &lt; b</code>" in result

    def test_fenced_code_block_no_lang(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        md = "```\nprint('hello')\n```"
        result = markdown_to_telegram_html(md)
        assert "<pre>" in result
        assert "print(&#x27;hello&#x27;)" in result

    def test_fenced_code_block_with_lang(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        md = "```python\nx = 1\n```"
        result = markdown_to_telegram_html(md)
        assert '<code class="language-python">' in result

    def test_code_block_content_not_double_escaped(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        md = "```\na < b && c > d\n```"
        result = markdown_to_telegram_html(md)
        # Should be escaped once, not double-escaped
        assert "a &lt; b &amp;&amp; c &gt; d" in result
        assert "&amp;lt;" not in result

    def test_heading_becomes_bold(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("# Title\n\nBody")
        assert "<b>Title</b>" in result

    def test_h2_heading(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("## Subtitle")
        assert "<b>Subtitle</b>" in result

    def test_link(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("[Click](https://example.com)")
        assert '<a href="https://example.com">Click</a>' in result

    def test_blockquote(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("> quoted text")
        assert "<blockquote>quoted text</blockquote>" in result

    def test_consecutive_blockquote_lines_grouped(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("> line1\n> line2")
        assert "<blockquote>line1\nline2</blockquote>" in result

    def test_horizontal_rule(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("above\n---\nbelow")
        assert "\u2014\u2014\u2014" in result  # em-dash triple

    def test_list_bullet(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("- item one\n- item two")
        assert "\u2022 item one" in result
        assert "\u2022 item two" in result

    def test_table_rendered_as_pre(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = markdown_to_telegram_html(md)
        assert "<pre>" in result

    def test_nested_bold_inside_heading(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("# **Title**")
        # Bold markers should be converted
        assert "<b>" in result

    def test_empty_string(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        result = markdown_to_telegram_html("")
        assert result == ""

    def test_mixed_formatting(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        md = "**bold** and *italic* and `code`"
        result = markdown_to_telegram_html(md)
        assert "<b>bold</b>" in result
        assert "<i>italic</i>" in result
        assert "<code>code</code>" in result

    def test_button_syntax_stripped_from_output(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        md = "Pick one:\n\n[button:Yes] [button:No]"
        result = markdown_to_telegram_html(md)
        assert "[button:" not in result
        assert "Pick one:" in result

    def test_multiple_button_lines_stripped(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        md = "Text\n\n[button:A]\n[button:B]\n[button:C]"
        result = markdown_to_telegram_html(md)
        assert "[button:" not in result
        assert "Text" in result

    def test_button_in_code_block_preserved(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        md = "```\n[button:NotReal]\n```\n\n[button:Real]"
        result = markdown_to_telegram_html(md)
        # The code block content should still have the button syntax (escaped)
        assert "[button:NotReal]" in result or "button:NotReal" in result
        # The real button outside code should be stripped
        assert "[button:Real]" not in result

    def test_button_in_inline_code_preserved(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        md = "Use `[button:syntax]` for buttons\n[button:Actual]"
        result = markdown_to_telegram_html(md)
        assert "button:syntax" in result
        assert "[button:Actual]" not in result

    def test_button_stripping_before_html_escape(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        md = "Choose <option>:\n\n[button:Go]"
        result = markdown_to_telegram_html(md)
        assert "[button:" not in result
        assert "&lt;option&gt;" in result

    def test_surrounding_text_intact_after_button_strip(self) -> None:
        from ductor_bot.bot.formatting import markdown_to_telegram_html

        md = "Before text.\n\n[button:Click]\n\nAfter text."
        result = markdown_to_telegram_html(md)
        assert "Before text." in result
        assert "After text." in result
        assert "[button:" not in result


class TestSplitHTMLMessage:
    """Test message splitting for Telegram's 4096 char limit."""

    def test_short_message_returns_single_chunk(self) -> None:
        from ductor_bot.bot.formatting import split_html_message

        result = split_html_message("Hello world")
        assert result == ["Hello world"]

    def test_split_on_paragraph_boundary(self) -> None:
        from ductor_bot.bot.formatting import split_html_message

        part1 = "A" * 3000
        part2 = "B" * 3000
        text = f"{part1}\n\n{part2}"
        result = split_html_message(text, max_len=4096)
        assert len(result) >= 2

    def test_split_on_newline_when_no_paragraph(self) -> None:
        from ductor_bot.bot.formatting import split_html_message

        lines = [f"Line {i}" for i in range(1000)]
        text = "\n".join(lines)
        result = split_html_message(text, max_len=4096)
        assert len(result) >= 2
        for chunk in result:
            assert len(chunk) <= 4096

    def test_hard_split_on_enormous_line(self) -> None:
        from ductor_bot.bot.formatting import split_html_message

        text = "X" * 10000
        result = split_html_message(text, max_len=4096)
        assert len(result) >= 3
        for chunk in result:
            assert len(chunk) <= 4096

    def test_empty_string(self) -> None:
        from ductor_bot.bot.formatting import split_html_message

        assert split_html_message("") == [""]

    def test_exact_limit(self) -> None:
        from ductor_bot.bot.formatting import split_html_message

        text = "X" * 4096
        result = split_html_message(text, max_len=4096)
        assert result == [text]

    def test_custom_max_len(self) -> None:
        from ductor_bot.bot.formatting import split_html_message

        text = "A" * 50
        result = split_html_message(text, max_len=20)
        assert len(result) >= 3
        for chunk in result:
            assert len(chunk) <= 20
