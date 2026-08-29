"""Scheduled wipe of the Consult topic.

Three kinds of residue outlive a consultation: the files in its directory, the
messages in the topic, and the CLI session keyed to that topic. Deleting the
files clears one of them. ``deleteForumTopic`` clears all three at once — it
removes every message with the topic, and the replacement topic gets a new id,
which is a new session key, so nothing can be resumed into it.

The schedule is user-chosen, so the pinned notice is generated from it rather
than promising "daily" regardless. A notice that misstates when data disappears
is worse than no notice: it is the sentence someone decides what to paste on.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError

from ductor_bot.i18n import t

if TYPE_CHECKING:
    from pathlib import Path

    from aiogram import Bot

    from ductor_bot.messenger.telegram.managed_topics import ManagedTopicStore

logger = logging.getLogger(__name__)

#: Schedule ids, in the order the picker shows them.
SCHEDULES = ("hourly", "6h", "daily", "weekly", "off")
DEFAULT_SCHEDULE = "daily"
DEFAULT_HOUR = 4

_EVERY_SIX_HOURS = 6
_WEEK = timedelta(days=7)


def schedule_label(schedule: str, hour: int) -> str:
    """Human wording for a schedule, used in the picker and the pinned notice."""
    if schedule == "off":
        return t("consult.schedule_off")
    if schedule == "hourly":
        return t("consult.schedule_hourly")
    if schedule == "6h":
        return t("consult.schedule_6h")
    if schedule == "weekly":
        return t("consult.schedule_weekly", hour=f"{hour:02d}")
    return t("consult.schedule_daily", hour=f"{hour:02d}")


def is_due(schedule: str, hour: int, now: datetime, last: datetime | None) -> bool:
    """Whether a wipe should run at *now*, given when one last ran.

    Called once an hour, so every branch only has to decide "is this the hour".
    """
    if schedule == "off":
        return False
    if last is None:
        # Never wiped: wait for a scheduled moment rather than firing at
        # startup, so a restart is not a data-loss event.
        return _at_scheduled_time(schedule, hour, now)
    if not _at_scheduled_time(schedule, hour, now):
        return False
    return not _already_ran(schedule, now, last)


def _at_scheduled_time(schedule: str, hour: int, now: datetime) -> bool:
    if schedule == "hourly":
        return True
    if schedule == "6h":
        return now.hour % _EVERY_SIX_HOURS == 0
    if schedule == "weekly":
        return now.weekday() == 0 and now.hour == hour
    return now.hour == hour


def _already_ran(schedule: str, now: datetime, last: datetime) -> bool:
    if schedule == "hourly":
        return (now - last) < timedelta(minutes=50)
    if schedule == "6h":
        return (now - last) < timedelta(hours=5)
    if schedule == "weekly":
        return (now - last) < _WEEK - timedelta(hours=1)
    return last.date() == now.date()


async def wipe_consult(
    bot: Bot, chat_id: int, store: ManagedTopicStore, consult_dir: Path
) -> bool:
    """Delete the Consult topic and its directory, then recreate both.

    Returns True when the wipe completed. The topic is deleted *first*: if that
    fails there is nothing to recreate, and continuing would leave a second
    Consult topic in the group with no way to find the first.
    """
    from ductor_bot.messenger.telegram.managed_topics import (
        CONSULT,
        TopicRecord,
        ensure_managed_topics,
    )

    record = store.get(chat_id, CONSULT)
    if record.topic_id is None:
        logger.debug("No Consult topic recorded for chat %d; nothing to wipe", chat_id)
        return False

    try:
        await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=record.topic_id)
    except TelegramAPIError:
        logger.warning(
            "Could not delete Consult topic %d in chat %d; leaving it alone",
            record.topic_id,
            chat_id,
            exc_info=True,
        )
        return False

    # Only now is the old topic definitely gone, so forgetting it cannot strand
    # a topic the store can no longer name.
    store.set(chat_id, CONSULT, TopicRecord())
    try:
        shutil.rmtree(consult_dir, ignore_errors=True)
    except OSError:
        logger.warning("Could not clear %s", consult_dir, exc_info=True)

    await ensure_managed_topics(bot, chat_id, store, consult_dir)
    logger.info("Wiped and recreated the Consult topic in chat %d", chat_id)
    return True
