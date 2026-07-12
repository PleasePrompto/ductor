from __future__ import annotations

from ductor_bot.messenger.slack.id_map import SlackIdMap


def test_channel_and_thread_mapping_round_trip(tmp_path) -> None:
    id_map = SlackIdMap(tmp_path)

    chat_id = id_map.channel_to_int("C123")
    topic_id = id_map.thread_to_int("C123", "1710000000.123")

    assert id_map.int_to_channel(chat_id) == "C123"
    assert id_map.int_to_thread(topic_id) == ("C123", "1710000000.123")
