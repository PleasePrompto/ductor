"""Tests for TaskRegistry."""

from __future__ import annotations

from pathlib import Path

import pytest

from ductor_bot.tasks.models import TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry


@pytest.fixture
def registry(tmp_path: Path) -> TaskRegistry:
    return TaskRegistry(
        registry_path=tmp_path / "tasks.json",
        tasks_dir=tmp_path / "tasks",
    )


def _submit(prompt: str = "test prompt", name: str = "") -> TaskSubmit:
    return TaskSubmit(
        chat_id=42,
        prompt=prompt,
        message_id=1,
        thread_id=None,
        parent_agent="main",
        name=name,
    )


class TestCreate:
    def test_creates_entry_and_folder(self, registry: TaskRegistry, tmp_path: Path) -> None:
        entry = registry.create(_submit("build website", name="Website"), "claude", "opus")
        assert entry.status == "running"
        assert entry.name == "Website"
        assert entry.provider == "claude"
        assert entry.prompt_preview == "build website"

        # Task folder and TASKMEMORY.md created
        folder = registry.task_folder(entry.task_id)
        assert folder.is_dir()
        assert registry.taskmemory_path(entry.task_id).is_file()

    def test_auto_name_from_id(self, registry: TaskRegistry) -> None:
        entry = registry.create(_submit(), "claude", "opus")
        assert entry.name == entry.task_id  # Fallback to task_id

    def test_persists_to_json(self, registry: TaskRegistry, tmp_path: Path) -> None:
        registry.create(_submit(name="A"), "claude", "opus")
        registry.create(_submit(name="B"), "codex", "gpt-4.1")

        # Reload and verify
        reg2 = TaskRegistry(
            registry_path=tmp_path / "tasks.json",
            tasks_dir=tmp_path / "tasks",
        )
        assert len(reg2.list_all()) == 2


class TestGet:
    def test_get_existing(self, registry: TaskRegistry) -> None:
        entry = registry.create(_submit(), "claude", "opus")
        assert registry.get(entry.task_id) is not None

    def test_get_missing(self, registry: TaskRegistry) -> None:
        assert registry.get("nonexistent") is None


class TestFindByName:
    def test_finds_by_name(self, registry: TaskRegistry) -> None:
        registry.create(_submit(name="Hotel Paris"), "claude", "opus")
        found = registry.find_by_name(42, "hotel paris")
        assert found is not None
        assert found.name == "Hotel Paris"

    def test_not_found_wrong_chat(self, registry: TaskRegistry) -> None:
        registry.create(_submit(name="Test"), "claude", "opus")
        assert registry.find_by_name(999, "Test") is None


class TestUpdateStatus:
    def test_updates_status_and_fields(self, registry: TaskRegistry) -> None:
        entry = registry.create(_submit(), "claude", "opus")
        registry.update_status(entry.task_id, "done", elapsed_seconds=5.0, error="")
        updated = registry.get(entry.task_id)
        assert updated is not None
        assert updated.status == "done"
        assert updated.elapsed_seconds == 5.0

    def test_ignores_unknown_task(self, registry: TaskRegistry) -> None:
        registry.update_status("bogus", "done")  # Should not raise


class TestListActive:
    def test_filters_running(self, registry: TaskRegistry) -> None:
        e1 = registry.create(_submit(name="A"), "claude", "opus")
        registry.create(_submit(name="B"), "claude", "opus")
        registry.update_status(e1.task_id, "done")

        active = registry.list_active(chat_id=42)
        assert len(active) == 1
        assert active[0].name == "B"


class TestCleanupOld:
    def test_removes_old_completed(self, registry: TaskRegistry) -> None:
        entry = registry.create(_submit(), "claude", "opus")
        registry.update_status(entry.task_id, "done")
        # Manually set old timestamp
        entry.created_at = 0.0
        registry._persist()

        removed = registry.cleanup_old(max_age_hours=1)
        assert removed == 1
        assert registry.get(entry.task_id) is None

    def test_keeps_recent(self, registry: TaskRegistry) -> None:
        entry = registry.create(_submit(), "claude", "opus")
        registry.update_status(entry.task_id, "done")

        removed = registry.cleanup_old(max_age_hours=1)
        assert removed == 0


class TestCleanupFinished:
    def test_removes_all_finished(self, registry: TaskRegistry) -> None:
        e1 = registry.create(_submit(name="A"), "claude", "opus")
        e2 = registry.create(_submit(name="B"), "claude", "opus")
        e3 = registry.create(_submit(name="C"), "claude", "opus")
        registry.update_status(e1.task_id, "done")
        registry.update_status(e2.task_id, "failed", error="oops")
        # e3 stays running

        removed = registry.cleanup_finished(chat_id=42)
        assert removed == 2
        assert registry.get(e1.task_id) is None
        assert registry.get(e2.task_id) is None
        assert registry.get(e3.task_id) is not None

    def test_scoped_to_chat(self, registry: TaskRegistry) -> None:
        e1 = registry.create(_submit(name="A"), "claude", "opus")
        registry.update_status(e1.task_id, "done")

        # Different chat_id -> nothing removed
        removed = registry.cleanup_finished(chat_id=999)
        assert removed == 0
        assert registry.get(e1.task_id) is not None

    def test_removes_task_folder(self, registry: TaskRegistry) -> None:
        entry = registry.create(_submit(name="X"), "claude", "opus")
        folder = registry.task_folder(entry.task_id)
        assert folder.is_dir()

        registry.update_status(entry.task_id, "cancelled")
        registry.cleanup_finished(chat_id=42)
        assert not folder.exists()

    def test_noop_when_empty(self, registry: TaskRegistry) -> None:
        assert registry.cleanup_finished() == 0


class TestCleanupFinishedRetention:
    def test_removes_finished_tasks_older_than_retention(self, registry: TaskRegistry) -> None:
        old_done = registry.create(_submit(name="old"), "claude", "opus")
        recent_done = registry.create(_submit(name="recent"), "claude", "opus")
        running = registry.create(_submit(name="running"), "claude", "opus")
        waiting = registry.create(_submit(name="waiting"), "claude", "opus")

        registry.update_status(old_done.task_id, "done")
        registry.update_status(recent_done.task_id, "done")
        registry.update_status(waiting.task_id, "waiting")
        old_done.completed_at = 100.0
        old_done.created_at = 100.0
        recent_done.completed_at = 10_000.0
        recent_done.created_at = 10_000.0
        running.created_at = 100.0
        waiting.created_at = 100.0
        registry._persist()

        removed = registry.cleanup_finished_retention(
            max_age_hours=1,
            keep_last=100,
            now=10_000.0,
        )

        assert removed == 1
        assert registry.get(old_done.task_id) is None
        assert registry.get(recent_done.task_id) is not None
        assert registry.get(running.task_id) is not None
        assert registry.get(waiting.task_id) is not None

    def test_keeps_only_latest_finished_when_count_exceeds_limit(
        self, registry: TaskRegistry
    ) -> None:
        entries = [registry.create(_submit(name=f"done-{i}"), "claude", "opus") for i in range(4)]
        for i, entry in enumerate(entries):
            registry.update_status(entry.task_id, "done")
            entry.completed_at = float(i + 1)
            entry.created_at = float(i + 1)
        registry._persist()

        removed = registry.cleanup_finished_retention(
            max_age_hours=24,
            keep_last=2,
            now=10.0,
        )

        assert removed == 2
        assert registry.get(entries[0].task_id) is None
        assert registry.get(entries[1].task_id) is None
        assert registry.get(entries[2].task_id) is not None
        assert registry.get(entries[3].task_id) is not None

    def test_removes_finished_task_folder(self, registry: TaskRegistry) -> None:
        entry = registry.create(_submit(name="old"), "claude", "opus")
        folder = registry.task_folder(entry.task_id)
        registry.update_status(entry.task_id, "failed")
        entry.completed_at = 1.0
        registry._persist()

        removed = registry.cleanup_finished_retention(
            max_age_hours=1,
            keep_last=100,
            now=10_000.0,
        )

        assert removed == 1
        assert not folder.exists()


class TestDelete:
    def test_deletes_finished_task(self, registry: TaskRegistry, tmp_path: Path) -> None:
        entry = registry.create(_submit(name="Deletable"), "claude", "opus")
        folder = registry.task_folder(entry.task_id)
        assert folder.is_dir()

        registry.update_status(entry.task_id, "done")
        assert registry.delete(entry.task_id) is True
        assert registry.get(entry.task_id) is None
        assert not folder.exists()

    def test_rejects_running_task(self, registry: TaskRegistry) -> None:
        entry = registry.create(_submit(name="Active"), "claude", "opus")
        assert registry.delete(entry.task_id) is False
        assert registry.get(entry.task_id) is not None

    def test_rejects_waiting_task(self, registry: TaskRegistry) -> None:
        entry = registry.create(_submit(name="Waiting"), "claude", "opus")
        registry.update_status(entry.task_id, "waiting")
        assert registry.delete(entry.task_id) is False

    def test_returns_false_for_missing(self, registry: TaskRegistry) -> None:
        assert registry.delete("nonexistent") is False

    def test_deletes_all_finished_statuses(self, registry: TaskRegistry) -> None:
        for status in ("done", "failed", "cancelled"):
            entry = registry.create(_submit(name=status), "claude", "opus")
            registry.update_status(entry.task_id, status)
            assert registry.delete(entry.task_id) is True

    def test_deletes_subagent_task_folder(self, registry: TaskRegistry, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agents" / "sub1" / "workspace" / "tasks"
        entry = registry.create(_submit(name="SubTask"), "codex", "gpt-5.2", tasks_dir=agent_dir)
        folder = registry.task_folder(entry.task_id)
        assert str(agent_dir) in str(folder)
        assert folder.is_dir()

        registry.update_status(entry.task_id, "done")
        assert registry.delete(entry.task_id) is True
        assert not folder.exists()


class TestLoadRecovery:
    def test_downgrades_stale_running(self, registry: TaskRegistry, tmp_path: Path) -> None:
        entry = registry.create(_submit(), "claude", "opus")
        assert entry.status == "running"

        # Simulate restart
        reg2 = TaskRegistry(
            registry_path=tmp_path / "tasks.json",
            tasks_dir=tmp_path / "tasks",
        )
        loaded = reg2.get(entry.task_id)
        assert loaded is not None
        assert loaded.status == "failed"
        assert "restarted" in loaded.error.lower()


class TestCleanupOrphans:
    def test_entry_without_folder_removed(self, tmp_path: Path) -> None:
        """Registry entry whose task folder was deleted → entry dropped."""
        reg = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
        entry = reg.create(_submit(name="Ghost"), "claude", "opus")

        # Delete the folder behind the registry's back
        import shutil

        shutil.rmtree(reg.task_folder(entry.task_id))

        # Reload — orphan cleanup should drop the entry
        reg2 = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
        assert reg2.get(entry.task_id) is None

    def test_folder_without_entry_removed(self, tmp_path: Path) -> None:
        """Task folder with no matching registry entry → folder deleted."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir(parents=True)
        orphan = tasks_dir / "deadbeef"
        orphan.mkdir()
        (orphan / "TASKMEMORY.md").write_text("leftover")

        # Create registry (no entries) — orphan folder should be removed
        TaskRegistry(tmp_path / "tasks.json", tasks_dir)
        assert not orphan.exists()

    def test_valid_entries_untouched(self, tmp_path: Path) -> None:
        """Entries with matching folders survive orphan cleanup."""
        reg = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
        entry = reg.create(_submit(name="Valid"), "claude", "opus")

        # Reload — entry and folder both exist, nothing removed
        reg2 = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
        assert reg2.get(entry.task_id) is not None
        assert reg2.task_folder(entry.task_id).is_dir()


class TestPerAgentTasksDir:
    """Task folder isolation for sub-agents."""

    def test_create_with_custom_tasks_dir(self, registry: TaskRegistry, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agents" / "test" / "workspace" / "tasks"
        entry = registry.create(_submit(name="Agent Task"), "codex", "gpt-5.2", tasks_dir=agent_dir)

        # Folder created in agent's workspace, not default
        folder = registry.task_folder(entry.task_id)
        assert str(agent_dir) in str(folder)
        assert folder.is_dir()
        assert (folder / "TASKMEMORY.md").is_file()

        # Default tasks dir NOT used
        default_folder = tmp_path / "tasks" / entry.task_id
        assert not default_folder.exists()

    def test_tasks_dir_persisted_and_reloaded(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agents" / "sub1" / "workspace" / "tasks"
        reg = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
        entry = reg.create(_submit(name="Persistent"), "claude", "opus", tasks_dir=agent_dir)

        # Reload registry — stored tasks_dir should be used
        reg2 = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
        loaded = reg2.get(entry.task_id)
        assert loaded is not None
        assert loaded.tasks_dir == str(agent_dir)

        folder = reg2.task_folder(entry.task_id)
        assert str(agent_dir) in str(folder)
        assert folder.is_dir()

    def test_default_tasks_dir_when_none(self, registry: TaskRegistry, tmp_path: Path) -> None:
        """Without override, task folder uses default tasks_dir."""
        entry = registry.create(_submit(name="Default"), "claude", "opus")
        folder = registry.task_folder(entry.task_id)
        assert str(tmp_path / "tasks") in str(folder)

    def test_cleanup_orphans_scans_agent_dirs(self, tmp_path: Path) -> None:
        """Orphan cleanup scans per-agent task dirs too."""
        agent_dir = tmp_path / "agents" / "sub1" / "workspace" / "tasks"
        reg = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
        entry = reg.create(_submit(name="Agent"), "claude", "opus", tasks_dir=agent_dir)

        # Create an orphan folder in the agent's tasks dir
        orphan = agent_dir / "deadbeef"
        orphan.mkdir(parents=True)
        (orphan / "TASKMEMORY.md").write_text("leftover")

        removed = reg.cleanup_orphans()
        assert removed == 1
        assert not orphan.exists()
        # Real entry folder untouched
        assert reg.task_folder(entry.task_id).is_dir()

    def test_cleanup_removes_agent_entry_without_folder(self, tmp_path: Path) -> None:
        """Entry with agent tasks_dir whose folder is missing → dropped."""
        import shutil

        agent_dir = tmp_path / "agents" / "sub1" / "workspace" / "tasks"
        reg = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
        entry = reg.create(_submit(name="Ghost"), "claude", "opus", tasks_dir=agent_dir)

        # Delete folder behind registry's back
        shutil.rmtree(reg.task_folder(entry.task_id))

        # Reload — orphan cleanup should drop the entry
        reg2 = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
        assert reg2.get(entry.task_id) is None
