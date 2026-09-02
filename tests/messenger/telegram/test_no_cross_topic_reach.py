"""No command may act on a conversation other than the one it was sent in.

`/new @topicname` reset another topic's session without entering it. It was a
convenience that quietly undid the property the whole session model exists to
provide: a topic is a machine of its own, and what happens to it happens in it.
The command is gone, and so is the reverse lookup that made addressing another
topic by name possible — a capability nothing else needed.
"""

from __future__ import annotations

import re
from pathlib import Path

from ductor_bot.messenger.telegram.topic import TopicNameCache

TELEGRAM = Path(__file__).resolve().parents[3] / "ductor_bot" / "messenger" / "telegram"


def test_a_topic_cannot_be_addressed_by_name() -> None:
    """Resolving a name to someone else's topic id is the whole mechanism."""
    cache = TopicNameCache()

    assert not hasattr(cache, "find_by_name")


def test_the_cache_still_names_a_topic_you_are_in() -> None:
    """Forward lookup is what headers and logs use; only the reverse one went."""
    cache = TopicNameCache()
    cache.set(-100, 110, "Salam-Website")

    assert cache.resolve(-100, 110) == "Salam-Website"


def test_no_handler_takes_a_topic_argument() -> None:
    """A command parsing `@name` is how the reach-across would come back."""
    offenders = []
    for path in TELEGRAM.glob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'startswith\("@"\)', line):
                offenders.append(f"{path.name}:{i}")

    assert not offenders, f"a handler parses an @topic argument: {offenders}"


def test_the_command_is_gone() -> None:
    app = (TELEGRAM / "app.py").read_text(encoding="utf-8")

    assert 'Command("new"' not in app
    assert "_on_new" not in app
