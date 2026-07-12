"""Persistent mapping between Slack channel/thread IDs and internal ints."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from pathlib import Path

from ductor_bot.infra.atomic_io import atomic_text_save

logger = logging.getLogger(__name__)


class SlackIdMap:
    """Bidirectional channel/thread mapping for Slack conversations."""

    def __init__(self, store_path: Path) -> None:
        self._channel_to_int: dict[str, int] = {}
        self._int_to_channel: dict[int, str] = {}
        self._thread_to_int: dict[str, int] = {}
        self._int_to_thread: dict[int, tuple[str, str]] = {}
        self._path = store_path / "slack_id_map.json"
        self._load()

    def channel_to_int(self, channel_id: str) -> int:
        """Get or create a deterministic int for a Slack channel ID."""
        if channel_id in self._channel_to_int:
            return self._channel_to_int[channel_id]
        int_id = self._allocate(channel_id, existing=self._int_to_channel)
        self._channel_to_int[channel_id] = int_id
        self._int_to_channel[int_id] = channel_id
        self._save()
        return int_id

    def int_to_channel(self, chat_id: int) -> str | None:
        """Resolve an internal chat ID back to a Slack channel ID."""
        return self._int_to_channel.get(chat_id)

    def thread_to_int(self, channel_id: str, thread_ts: str) -> int:
        """Get or create a deterministic int for a Slack thread."""
        key = self._thread_key(channel_id, thread_ts)
        if key in self._thread_to_int:
            return self._thread_to_int[key]
        int_id = self._allocate(key, existing=dict(self._int_to_thread))
        self._thread_to_int[key] = int_id
        self._int_to_thread[int_id] = (channel_id, thread_ts)
        self._save()
        return int_id

    def int_to_thread(self, topic_id: int) -> tuple[str, str] | None:
        """Resolve an internal topic ID back to ``(channel_id, thread_ts)``."""
        return self._int_to_thread.get(topic_id)

    @staticmethod
    def _thread_key(channel_id: str, thread_ts: str) -> str:
        return f"{channel_id}:{thread_ts}"

    @staticmethod
    def _allocate(key: str, *, existing: Mapping[int, object]) -> int:
        int_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
        while int_id in existing:
            int_id = int.from_bytes(hashlib.sha256(f"{key}:{int_id}".encode()).digest()[:8], "big")
        return int_id

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            channels = data.get("channels", {})
            threads = data.get("threads", {})
            if isinstance(channels, dict):
                for channel_id, int_id in channels.items():
                    if isinstance(channel_id, str) and isinstance(int_id, int):
                        self._channel_to_int[channel_id] = int_id
                        self._int_to_channel[int_id] = channel_id
            if isinstance(threads, dict):
                for key, int_id in threads.items():
                    if not isinstance(key, str) or not isinstance(int_id, int):
                        continue
                    channel_id, _, thread_ts = key.partition(":")
                    if channel_id and thread_ts:
                        self._thread_to_int[key] = int_id
                        self._int_to_thread[int_id] = (channel_id, thread_ts)
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to load slack_id_map.json, starting fresh")

    def _save(self) -> None:
        payload = {
            "channels": self._channel_to_int,
            "threads": self._thread_to_int,
        }
        atomic_text_save(self._path, json.dumps(payload, indent=2, sort_keys=True))
