"""Background loop that wipes the Consult topic on its schedule.

Separate from ``CleanupObserver`` because it is not file retention: it deletes
a Telegram topic and everything said in it. Sharing a loop with the media sweep
would mean one failure mode could silently stop the other.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from ductor_bot.config import resolve_user_timezone
from ductor_bot.infra.base_observer import BaseObserver
from ductor_bot.messenger.telegram.consult_wipe import is_due, wipe_consult

if TYPE_CHECKING:
    from aiogram import Bot

    from ductor_bot.config import AgentConfig
    from ductor_bot.messenger.telegram.managed_topics import ManagedTopicStore
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

#: Re-check every hour. Every schedule the picker offers is at least hourly,
#: so this is the finest resolution any of them needs.
_CHECK_INTERVAL = 3600


class ConsultWipeObserver(BaseObserver):
    """Checks hourly whether the Consult topic is due to be wiped."""

    def __init__(
        self,
        config: AgentConfig,
        paths: DuctorPaths,
        bot: Bot,
        store: ManagedTopicStore,
    ) -> None:
        super().__init__()
        self._config = config
        self._paths = paths
        self._bot = bot
        self._store = store

    async def start(self) -> None:
        if not self._config.managed_topics or self._config.consult_wipe == "off":
            logger.info("Consult wipe disabled")
            return
        await super().start()
        logger.info(
            "Consult wipe started (schedule: %s, hour: %d:00)",
            self._config.consult_wipe,
            self._config.consult_wipe_hour,
        )

    async def stop(self) -> None:
        await super().stop()
        logger.info("Consult wipe stopped")

    async def _run(self) -> None:
        while self._running:
            await asyncio.sleep(_CHECK_INTERVAL)
            if not self._running or not self._config.managed_topics:
                continue
            try:
                await self._maybe_wipe()
            except Exception:
                # One bad cycle must not end the loop; the next hour retries.
                logger.exception("Consult wipe cycle failed")

    async def _maybe_wipe(self) -> None:
        now = datetime.now(self._timezone())
        for chat_id in self._config.allowed_group_ids:
            stamp = self._store.last_wipe(chat_id)
            last = datetime.fromtimestamp(stamp, self._timezone()) if stamp else None
            if not is_due(self._config.consult_wipe, self._config.consult_wipe_hour, now, last):
                continue
            wiped = await wipe_consult(
                self._bot, chat_id, self._store, self._paths.consult_dir
            )
            if wiped:
                self._store.mark_wiped(chat_id, int(time.time()))

    def _timezone(self):  # noqa: ANN202
        """The user's timezone: "4am" has to mean their 4am."""
        return resolve_user_timezone(self._config.user_timezone)
