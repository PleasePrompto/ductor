"""Contract tests for Telegram TaskHub progress configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ductor_bot.config import AgentConfig, deep_merge_config


def test_task_progress_defaults_are_enabled_at_thirty_seconds() -> None:
    tasks = AgentConfig().tasks

    assert tasks.progress_updates is True
    assert tasks.progress_interval_seconds == 30.0


def test_task_progress_interval_rejects_values_below_ten_seconds() -> None:
    with pytest.raises(ValidationError, match="progress_interval_seconds"):
        AgentConfig(tasks={"progress_interval_seconds": 9.9})

    assert (
        AgentConfig(tasks={"progress_interval_seconds": 10.0}).tasks.progress_interval_seconds
        == 10.0
    )


def test_existing_tasks_config_receives_progress_defaults_without_losing_values() -> None:
    defaults = AgentConfig().model_dump(mode="json")
    merged, changed = deep_merge_config({"tasks": {"enabled": False, "max_parallel": 2}}, defaults)

    tasks = merged["tasks"]
    assert isinstance(tasks, dict)
    assert changed is True
    assert tasks["enabled"] is False
    assert tasks["max_parallel"] == 2
    assert tasks["progress_updates"] is True
    assert tasks["progress_interval_seconds"] == 30.0


def test_config_example_documents_the_runtime_progress_defaults() -> None:
    root = Path(__file__).resolve().parents[2]
    example = json.loads((root / "config.example.json").read_text(encoding="utf-8"))

    assert example["tasks"]["progress_updates"] is True
    assert example["tasks"]["progress_interval_seconds"] == 30.0
