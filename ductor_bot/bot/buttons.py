"""Parse ``[button:text]`` markers from CLI output and build Telegram inline keyboards.

The pattern ``[button:Label]`` in assistant text is converted to an
:class:`~aiogram.types.InlineKeyboardMarkup`.  Buttons on the same line form
one row; buttons on separate lines form separate rows.

Two public helpers are exposed:

* :func:`extract_buttons` -- parse + build keyboard (used at finalize time)
* :func:`strip_button_syntax` -- remove markers from display text (used during streaming)
"""

from __future__ import annotations

import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_BUTTON_RE = re.compile(r"\[button:([^\]]+)\]")
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

_CALLBACK_DATA_MAX_BYTES = 64


def _truncate_callback_data(text: str) -> str:
    """Truncate *text* so its UTF-8 encoding fits within 64 bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= _CALLBACK_DATA_MAX_BYTES:
        return text
    truncated = encoded[:_CALLBACK_DATA_MAX_BYTES]
    return truncated.decode("utf-8", errors="ignore")


def _mask_code(text: str) -> tuple[str, list[str]]:
    """Replace code blocks and inline code with placeholders.

    Returns the masked text and a list of original code snippets.
    """
    saved: list[str] = []

    def _save(m: re.Match[str]) -> str:
        idx = len(saved)
        saved.append(m.group(0))
        return f"\x00CODE{idx}\x00"

    masked = _CODE_BLOCK_RE.sub(_save, text)
    masked = _INLINE_CODE_RE.sub(_save, masked)
    return masked, saved


def _restore_code(text: str, saved: list[str]) -> str:
    """Restore code placeholders with original content."""
    for i, original in enumerate(saved):
        text = text.replace(f"\x00CODE{i}\x00", original)
    return text


def _collapse_blank_lines(text: str) -> str:
    """Replace three or more consecutive newlines with exactly two."""
    return re.sub(r"\n{3,}", "\n\n", text)


def extract_buttons(text: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Extract ``[button:...]`` markers and return cleaned text + keyboard.

    Returns:
        A tuple of (cleaned_text, markup).  *markup* is ``None`` when no
        valid buttons were found.
    """
    if not text or "[button:" not in text:
        return text, None

    masked, saved = _mask_code(text)

    rows: list[list[InlineKeyboardButton]] = []

    def _process_line(line: str) -> str:
        matches = list(_BUTTON_RE.finditer(line))
        if not matches:
            return line
        btns: list[InlineKeyboardButton] = []
        for m in matches:
            label = m.group(1).strip()
            if not label:
                continue
            btns.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=_truncate_callback_data(label),
                ),
            )
        if btns:
            rows.append(btns)
        return _BUTTON_RE.sub("", line)

    cleaned_lines = [_process_line(line) for line in masked.split("\n")]
    cleaned = "\n".join(cleaned_lines)
    cleaned = _restore_code(cleaned, saved)
    cleaned = _collapse_blank_lines(cleaned).strip()

    if not rows:
        return cleaned, None

    return cleaned, InlineKeyboardMarkup(inline_keyboard=rows)


def strip_button_syntax(text: str) -> str:
    """Remove ``[button:...]`` markers from *text*, preserving code blocks.

    Used by the formatting pipeline during streaming so button syntax
    never appears as visible text in Telegram messages.
    """
    if not text or "[button:" not in text:
        return text

    masked, saved = _mask_code(text)
    stripped = _BUTTON_RE.sub("", masked)
    restored = _restore_code(stripped, saved)
    return _collapse_blank_lines(restored).strip()
