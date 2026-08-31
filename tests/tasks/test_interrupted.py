"""What happens to work that a restart caught mid-flight.

The rule this encodes: a task interrupted by a restart is announced to the
topic that started it and never resumed automatically. The registry cannot tell
a worker that finished its side effects from one that had not begun them — the
same row of JSON describes both — so re-running is a coin flip that can publish
twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ductor_bot.tasks.models import TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry


def _submit(thread_id: int | None = 110) -> TaskSubmit:
    return TaskSubmit(
        chat_id=-1004326514872,
        prompt="Add the deck photos to the About page",
        message_id=1,
        thread_id=thread_id,
        parent_agent="main",
        name="Deck photos",
    )


@pytest.fixture
def registry(tmp_path: Path) -> TaskRegistry:
    return TaskRegistry(registry_path=tmp_path / "tasks.json", tasks_dir=tmp_path / "tasks")


def _reload(tmp_path: Path) -> TaskRegistry:
    return TaskRegistry(registry_path=tmp_path / "tasks.json", tasks_dir=tmp_path / "tasks")


def test_a_restart_reports_the_task_as_interrupted(
    registry: TaskRegistry, tmp_path: Path
) -> None:
    entry = registry.create(_submit(), "claude", "sonnet")

    reloaded = _reload(tmp_path).get(entry.task_id)

    assert reloaded is not None
    assert reloaded.status == "interrupted"


def test_the_interrupted_list_is_offered_once(registry: TaskRegistry, tmp_path: Path) -> None:
    """Announcing on every reload would train the user to ignore the notice."""
    registry.create(_submit(), "claude", "sonnet")
    reloaded = _reload(tmp_path)

    assert len(reloaded.take_interrupted_by_restart()) == 1
    assert reloaded.take_interrupted_by_restart() == []


def test_a_finished_task_is_never_reported_as_interrupted(
    registry: TaskRegistry, tmp_path: Path
) -> None:
    """The case that must never regress: no completed work gets re-offered."""
    entry = registry.create(_submit(), "claude", "sonnet")
    registry.update_status(entry.task_id, "done")

    reloaded = _reload(tmp_path)

    assert reloaded.get(entry.task_id).status == "done"
    assert reloaded.take_interrupted_by_restart() == []


def test_cancelled_and_failed_tasks_are_left_alone(
    registry: TaskRegistry, tmp_path: Path
) -> None:
    for status in ("failed", "cancelled"):
        entry = registry.create(_submit(), "claude", "sonnet")
        registry.update_status(entry.task_id, status)

        reloaded = _reload(tmp_path)

        assert reloaded.get(entry.task_id).status == status
        assert entry.task_id not in reloaded.take_interrupted_by_restart()


def test_the_entry_keeps_the_topic_it_came_from(registry: TaskRegistry, tmp_path: Path) -> None:
    """The notice has to reach the topic that started the work, not General."""
    entry = registry.create(_submit(thread_id=110), "claude", "sonnet")

    reloaded = _reload(tmp_path).get(entry.task_id)

    assert reloaded is not None
    assert reloaded.thread_id == 110
