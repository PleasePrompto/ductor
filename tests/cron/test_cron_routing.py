"""Tests for cron job routing: chat_id/topic_id on CronJob and result delivery."""

from __future__ import annotations

from ductor_bot.cron.manager import CronJob


class TestCronJobRoutingFields:
    def test_cron_job_stores_chat_id_topic_id(self) -> None:
        job = CronJob(
            id="j1",
            title="T",
            description="D",
            schedule="* * * * *",
            task_folder="f",
            agent_instruction="do stuff",
            chat_id="12345",
            topic_id="99",
        )
        d = job.to_dict()
        assert d["chat_id"] == "12345"
        assert d["topic_id"] == "99"
        restored = CronJob.from_dict(d)
        assert restored.chat_id == "12345"
        assert restored.topic_id == "99"

    def test_cron_job_defaults_to_empty(self) -> None:
        job = CronJob(
            id="j2",
            title="T",
            description="D",
            schedule="* * * * *",
            task_folder="f",
            agent_instruction="do stuff",
        )
        assert job.chat_id == ""
        assert job.topic_id is None

    def test_legacy_job_without_routing_fields(self) -> None:
        d = {
            "id": "old",
            "title": "T",
            "description": "D",
            "schedule": "* * * * *",
            "task_folder": "f",
            "agent_instruction": "x",
        }
        job = CronJob.from_dict(d)
        assert job.chat_id == ""
        assert job.topic_id is None
