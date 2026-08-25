"""Transport-agnostic button and selector response types."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Button:
    """A single interactive button.

    ``copy_text`` asks the transport to copy that string to the user's clipboard
    instead of firing a callback — used to hand over a slash command the user
    then edits before sending. Transports without the feature ignore it and fall
    back to ``callback_data``.
    """

    text: str
    callback_data: str
    copy_text: str | None = None


@dataclass(frozen=True, slots=True)
class ButtonGrid:
    """Grid of buttons (list of rows, each row is a list of buttons)."""

    rows: list[list[Button]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SelectorResponse:
    """Result from a selector function: display text + optional buttons."""

    text: str
    buttons: ButtonGrid | None = None
