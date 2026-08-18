"""Tests for task priority levels (#79)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.tasks.hub import TaskHub
from ductor_bot.tasks.models import TASK_PRIORITIES, TaskEntry, TaskSubmit, normalise_priority
from ductor_bot.tasks.registry import TaskRegistry

# -- Model tests -------------------------------------------------------------


def test_task_submit_default_priority() -> None:
    submit = TaskSubmit(
        chat_id=1,
        prompt="p",
        message_id=0,
        thread_id=None,
        parent_agent="main",
    )
    assert submit.priority == "background"


def test_task_entry_default_priority() -> None:
    entry = TaskEntry(
        task_id="abc",
        chat_id=1,
        parent_agent="main",
        name="n",
        prompt_preview="p",
        provider="claude",
        model="opus",
        status="running",
    )
    assert entry.priority == "background"


def test_task_entry_round_trip_preserves_priority() -> None:
    entry = TaskEntry(
        task_id="abc",
        chat_id=1,
        parent_agent="main",
        name="n",
        prompt_preview="p",
        provider="claude",
        model="opus",
        status="running",
        priority="interactive",
    )
    data = entry.to_dict()
    assert data["priority"] == "interactive"

    revived = TaskEntry.from_dict(data)
    assert revived.priority == "interactive"


def test_task_entry_from_dict_unknown_priority_falls_back() -> None:
    raw = {
        "task_id": "abc",
        "chat_id": 1,
        "parent_agent": "main",
        "name": "n",
        "prompt_preview": "p",
        "provider": "claude",
        "model": "opus",
        "status": "running",
        "priority": "typo-value",
    }
    revived = TaskEntry.from_dict(raw)
    assert revived.priority == "background"


def test_task_entry_from_dict_missing_priority_defaults_to_background() -> None:
    raw = {
        "task_id": "abc",
        "chat_id": 1,
        "parent_agent": "main",
        "name": "n",
        "prompt_preview": "p",
        "provider": "claude",
        "model": "opus",
        "status": "running",
    }
    revived = TaskEntry.from_dict(raw)
    assert revived.priority == "background"


def test_normalise_priority_accepts_all_levels() -> None:
    for level in TASK_PRIORITIES:
        assert normalise_priority(level) == level


def test_normalise_priority_rejects_unknown() -> None:
    assert normalise_priority("nope") == "background"
    assert normalise_priority("") == "background"
    assert normalise_priority(None) == "background"


# -- Hub cap-bypass tests ----------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> TaskRegistry:
    return TaskRegistry(
        registry_path=tmp_path / "tasks.json",
        tasks_dir=tmp_path / "tasks",
    )


def _make_config(max_parallel: int = 1) -> MagicMock:
    config = MagicMock()
    config.enabled = True
    config.max_parallel = max_parallel
    config.timeout_seconds = 60.0
    return config


def _make_cli_service() -> MagicMock:
    cli = MagicMock()
    response = MagicMock()
    response.result = "ok"
    response.session_id = "s1"
    response.is_error = False
    response.timed_out = False
    response.num_turns = 1
    cli.execute = AsyncMock(return_value=response)

    async def _streaming(request: object, **_: object) -> MagicMock:
        return await cli.execute(request)

    cli.execute_streaming = AsyncMock(side_effect=_streaming)
    cli.resolve_provider = MagicMock(return_value=("claude", "opus"))
    return cli


def _submit(priority: str = "background", prompt: str = "x") -> TaskSubmit:
    return TaskSubmit(
        chat_id=42,
        prompt=prompt,
        message_id=1,
        thread_id=None,
        parent_agent="main",
        name=f"Task-{priority}",
        priority=priority,
    )


async def test_interactive_bypasses_cap(registry: TaskRegistry, tmp_path: Path) -> None:
    hub = TaskHub(
        registry,
        MagicMock(workspace=tmp_path),
        cli_service=_make_cli_service(),
        config=_make_config(max_parallel=1),
    )
    # Fill the cap with a background task.
    bg_id = hub.submit(_submit(priority="background", prompt="bg"))
    assert registry.get(bg_id) is not None

    # Interactive task must still be accepted even though the cap is full.
    interactive_id = hub.submit(_submit(priority="interactive", prompt="int"))
    entry = registry.get(interactive_id)
    assert entry is not None
    assert entry.priority == "interactive"

    await hub.shutdown()


async def test_batch_respects_cap(registry: TaskRegistry, tmp_path: Path) -> None:
    hub = TaskHub(
        registry,
        MagicMock(workspace=tmp_path),
        cli_service=_make_cli_service(),
        config=_make_config(max_parallel=1),
    )
    hub.submit(_submit(priority="background", prompt="bg"))

    with pytest.raises(ValueError, match="Too many"):
        hub.submit(_submit(priority="batch", prompt="bt"))

    await hub.shutdown()


async def test_background_respects_cap(registry: TaskRegistry, tmp_path: Path) -> None:
    hub = TaskHub(
        registry,
        MagicMock(workspace=tmp_path),
        cli_service=_make_cli_service(),
        config=_make_config(max_parallel=1),
    )
    hub.submit(_submit(priority="background", prompt="bg1"))

    with pytest.raises(ValueError, match="Too many"):
        hub.submit(_submit(priority="background", prompt="bg2"))

    await hub.shutdown()


async def test_interactive_does_not_count_against_cap_for_batch(
    registry: TaskRegistry, tmp_path: Path
) -> None:
    """An already-running interactive task does not block a batch submit."""
    hub = TaskHub(
        registry,
        MagicMock(workspace=tmp_path),
        cli_service=_make_cli_service(),
        config=_make_config(max_parallel=1),
    )
    # Running interactive task fills "the slot" but should be excluded.
    hub.submit(_submit(priority="interactive", prompt="int"))

    # A batch submit now should still succeed — the interactive does not count.
    batch_id = hub.submit(_submit(priority="batch", prompt="bt"))
    assert registry.get(batch_id) is not None

    await hub.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
