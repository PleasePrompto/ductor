"""Re-injection after a compaction boundary.

Compaction keeps the session id, so nothing else in the system would notice that
the model has just lost its context. Without this flag the consolidation writes
a careful handoff and then no one reads it.
"""

from __future__ import annotations

from ductor_bot.handoff.reinject import ReinjectFlags
from ductor_bot.session.key import SessionKey

KEY = SessionKey.telegram(chat_id=-100, topic_id=110)
OTHER = SessionKey.telegram(chat_id=-100, topic_id=97)


def test_a_marked_conversation_is_taken_once() -> None:
    flags = ReinjectFlags()
    flags.mark(KEY)

    assert flags.take(KEY)
    assert not flags.take(KEY)


def test_an_unmarked_conversation_is_never_owed_one() -> None:
    assert not ReinjectFlags().take(KEY)


def test_marks_do_not_leak_between_conversations() -> None:
    flags = ReinjectFlags()
    flags.mark(KEY)

    assert not flags.take(OTHER)
    assert flags.take(KEY)


def test_marking_twice_still_takes_once() -> None:
    flags = ReinjectFlags()
    flags.mark(KEY)
    flags.mark(KEY)

    assert flags.take(KEY)
    assert not flags.take(KEY)


def test_general_and_a_topic_are_distinct() -> None:
    flags = ReinjectFlags()
    general = SessionKey.telegram(chat_id=-100)
    flags.mark(general)

    assert not flags.take(KEY)
    assert flags.take(general)
