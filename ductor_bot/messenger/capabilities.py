"""Declares what each messenger transport supports."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MessengerCapabilities:
    """Feature matrix for a messenger transport."""

    name: str = ""
    supports_inline_buttons: bool = False
    supports_reactions: bool = False
    supports_message_editing: bool = False
    supports_threads: bool = False
    supports_typing_indicator: bool = True
    supports_file_send: bool = True
    supports_streaming_edit: bool = False
    supports_seen_indicator: bool = False
    max_message_length: int = 4096


TELEGRAM_CAPABILITIES = MessengerCapabilities(
    name="telegram",
    supports_inline_buttons=True,
    supports_reactions=False,
    supports_message_editing=True,
    supports_threads=True,
    supports_typing_indicator=True,
    supports_file_send=True,
    supports_streaming_edit=True,
    supports_seen_indicator=True,
    max_message_length=4096,
)

MATRIX_CAPABILITIES = MessengerCapabilities(
    name="matrix",
    supports_inline_buttons=False,
    supports_reactions=True,
    supports_message_editing=False,
    supports_threads=False,
    supports_typing_indicator=True,
    supports_file_send=True,
    supports_streaming_edit=False,
    supports_seen_indicator=True,
    max_message_length=40000,
)

SLACK_CAPABILITIES = MessengerCapabilities(
    name="slack",
    supports_inline_buttons=False,
    supports_reactions=True,
    supports_message_editing=True,
    supports_threads=True,
    supports_typing_indicator=False,
    supports_file_send=True,
    supports_streaming_edit=False,
    supports_seen_indicator=False,
    max_message_length=40000,
)
