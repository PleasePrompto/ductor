"""Tests for task data models."""

from __future__ import annotations

from ductor_bot.tasks.models import TaskEntry, TaskResult, TaskSubmit


class TestTaskEntry:
    def test_to_dict_roundtrip(self) -> None:
        entry = TaskEntry(
            task_id="abc123",
            chat_id=42,
            parent_agent="main",
            name="Flugsuche Paris",
            prompt_preview="Suche Flüge nach Paris",
            provider="claude",
            model="opus",
            status="running",
            session_id="sess-1",
            created_at=1000.0,
            question_count=2,
        )
        d = entry.to_dict()
        restored = TaskEntry.from_dict(d)

        assert restored.task_id == "abc123"
        assert restored.chat_id == 42
        assert restored.parent_agent == "main"
        assert restored.name == "Flugsuche Paris"
        assert restored.provider == "claude"
        assert restored.model == "opus"
        assert restored.status == "running"
        assert restored.session_id == "sess-1"
        assert restored.question_count == 2

    def test_from_dict_defaults(self) -> None:
        d = {"task_id": "x", "chat_id": 1}
        entry = TaskEntry.from_dict(d)
        assert entry.parent_agent == "main"
        assert entry.name == ""
        assert entry.status == "running"
        assert entry.question_count == 0

    def test_reasoning_effort_roundtrip(self) -> None:
        entry = TaskEntry(
            task_id="e1",
            chat_id=1,
            parent_agent="main",
            name="t",
            prompt_preview="p",
            provider="claude",
            model="opus",
            status="running",
            reasoning_effort="high",
        )
        restored = TaskEntry.from_dict(entry.to_dict())
        assert restored.reasoning_effort == "high"

    def test_reasoning_effort_default_empty(self) -> None:
        entry = TaskEntry.from_dict({"task_id": "x", "chat_id": 1})
        assert entry.reasoning_effort == ""

    def test_to_dict_includes_original_prompt(self) -> None:
        """#90: original_prompt MUST be persisted so it survives bot restarts.

        Without this, only prompt_preview (80 chars) survives and the parent
        agent's injected-prompt ends with 'Original task:' and nothing after."""
        entry = TaskEntry(
            task_id="x",
            chat_id=1,
            parent_agent="main",
            name="test",
            prompt_preview="short",
            provider="claude",
            model="opus",
            status="done",
            original_prompt="very long prompt with all the context...",
        )
        d = entry.to_dict()
        assert d["original_prompt"] == "very long prompt with all the context..."

    def test_from_dict_original_prompt_roundtrip(self) -> None:
        """#90: to_dict -> from_dict preserves original_prompt across JSON."""
        entry = TaskEntry(
            task_id="r",
            chat_id=2,
            parent_agent="main",
            name="roundtrip",
            prompt_preview="preview",
            provider="claude",
            model="opus",
            status="done",
            original_prompt="THE FULL ORIGINAL PROMPT",
        )
        restored = TaskEntry.from_dict(entry.to_dict())
        assert restored.original_prompt == "THE FULL ORIGINAL PROMPT"

    def test_from_dict_original_prompt_defaults_empty(self) -> None:
        """#90 backward-compat: old tasks.json without original_prompt loads cleanly."""
        d = {"task_id": "old", "chat_id": 1}
        entry = TaskEntry.from_dict(d)
        assert entry.original_prompt == ""

    def test_thread_id_roundtrip(self) -> None:
        entry = TaskEntry(
            task_id="t1",
            chat_id=100,
            parent_agent="main",
            name="topic-task",
            prompt_preview="short",
            provider="claude",
            model="opus",
            status="running",
            thread_id=42,
        )
        d = entry.to_dict()
        assert d["thread_id"] == 42
        restored = TaskEntry.from_dict(d)
        assert restored.thread_id == 42

    def test_thread_id_none_omitted(self) -> None:
        entry = TaskEntry(
            task_id="t2",
            chat_id=1,
            parent_agent="main",
            name="",
            prompt_preview="",
            provider="",
            model="",
            status="running",
        )
        d = entry.to_dict()
        assert "thread_id" not in d
        restored = TaskEntry.from_dict(d)
        assert restored.thread_id is None


class TestTaskSubmit:
    def test_default_fields(self) -> None:
        sub = TaskSubmit(
            chat_id=1,
            prompt="do something",
            message_id=10,
            thread_id=None,
            parent_agent="main",
        )
        assert sub.name == ""
        assert sub.provider_override == ""
        assert sub.thinking_override == ""


class TestTaskResult:
    def test_fields(self) -> None:
        result = TaskResult(
            task_id="abc",
            chat_id=1,
            parent_agent="main",
            name="test",
            prompt_preview="short",
            result_text="done!",
            status="done",
            elapsed_seconds=5.0,
            provider="claude",
            model="opus",
        )
        assert result.status == "done"
        assert result.error == ""

    def test_thread_id_default(self) -> None:
        result = TaskResult(
            task_id="x",
            chat_id=1,
            parent_agent="main",
            name="t",
            prompt_preview="p",
            result_text="r",
            status="done",
            elapsed_seconds=0.0,
            provider="c",
            model="m",
        )
        assert result.thread_id is None

    def test_thread_id_set(self) -> None:
        result = TaskResult(
            task_id="x",
            chat_id=1,
            parent_agent="main",
            name="t",
            prompt_preview="p",
            result_text="r",
            status="done",
            elapsed_seconds=0.0,
            provider="c",
            model="m",
            thread_id=99,
        )
        assert result.thread_id == 99
