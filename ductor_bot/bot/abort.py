"""Abort trigger detection for the Telegram bot.

Recognises the ``/stop`` command and bare-word abort triggers in
English and German.
"""

from __future__ import annotations

ABORT_WORDS: frozenset[str] = frozenset(
    {
        # English
        "stop",
        "abort",
        "cancel",
        "halt",
        "hold",
        "wait",
        "quit",
        "exit",
        # Note: "esc" and "interrupt" are handled by is_interrupt_message()
        # German
        "stopp",
        "warte",
        "abbruch",
        "abbrechen",
        "aufhören",
    }
)


ABORT_ALL_PHRASES: frozenset[str] = frozenset(
    {
        "stop all",
        "stopp alle",
        "alles stoppen",
        "cancel all",
        "abort all",
    }
)


def is_abort_trigger(text: str) -> bool:
    """Return *True* if *text* is a single bare-word abort trigger."""
    stripped = text.strip().lower()
    if " " in stripped:
        return False
    return stripped in ABORT_WORDS


def is_abort_all_trigger(text: str) -> bool:
    """Return *True* if *text* is a multi-word "stop all" trigger."""
    return text.strip().lower() in ABORT_ALL_PHRASES


def is_abort_message(text: str) -> bool:
    """Return *True* if *text* is a ``/stop`` command or a bare-word abort."""
    stripped = text.strip()
    command = stripped.lower().split(None, 1)[0] if stripped else ""
    if command == "/stop" or command.startswith("/stop@"):
        return True
    return is_abort_trigger(stripped)


def is_abort_all_message(text: str) -> bool:
    """Return *True* if *text* is a ``/stop_all`` command or "stop all" phrase."""
    stripped = text.strip()
    command = stripped.lower().split(None, 1)[0] if stripped else ""
    if command == "/stop_all" or command.startswith("/stop_all@"):
        return True
    return is_abort_all_trigger(stripped)


# -- Interrupt detection (soft SIGINT, not kill) ------------------------------

INTERRUPT_WORDS: frozenset[str] = frozenset({"esc", "interrupt"})


def is_interrupt_trigger(text: str) -> bool:
    """Return *True* if *text* is a bare-word interrupt trigger."""
    stripped = text.strip().lower()
    if " " in stripped:
        return False
    return stripped in INTERRUPT_WORDS


def is_interrupt_message(text: str) -> bool:
    """Return *True* if *text* is a ``/interrupt`` command or bare-word interrupt."""
    stripped = text.strip()
    command = stripped.lower().split(None, 1)[0] if stripped else ""
    if command in ("/interrupt", "!interrupt") or command.startswith("/interrupt@"):
        return True
    return is_interrupt_trigger(stripped)
