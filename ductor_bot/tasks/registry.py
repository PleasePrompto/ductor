"""Task registry: persistent CRUD for background tasks."""

from __future__ import annotations

import logging
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from ductor_bot.infra.json_store import atomic_json_save, load_json
from ductor_bot.tasks.models import TaskEntry, TaskSubmit, normalise_priority

logger = logging.getLogger(__name__)

_PROMPT_PREVIEW_LEN = 80
_RESULT_PREVIEW_LEN = 200
_FINISHED_STATUSES = frozenset({"done", "failed", "cancelled"})


class TaskRegistry:
    """Persistent registry for background task metadata.

    Follows the same atomic-JSON pattern as ``NamedSessionRegistry``.
    On load, stale ``"running"`` entries are downgraded to ``"failed"``.
    """

    def __init__(self, registry_path: Path, tasks_dir: Path) -> None:
        self._path = registry_path
        self._tasks_dir = tasks_dir
        self._entries: dict[str, TaskEntry] = {}
        self._load()
        self._cleanup_orphans()

    def _load(self) -> None:
        data = load_json(self._path)
        if data is None:
            return
        for raw in data.get("tasks", []):
            try:
                entry = TaskEntry.from_dict(raw)
            except (KeyError, TypeError):
                logger.warning("Skipping corrupt task entry: %s", raw)
                continue
            if entry.status == "running":
                entry.status = "failed"
                entry.error = "Bot restarted while task was running"
                logger.info("Downgraded stale running task %s to failed", entry.task_id)
            self._entries[entry.task_id] = entry

    def cleanup_orphans(self) -> int:
        """Remove orphaned entries and folders so nothing is left dangling.

        Called at startup and periodically.  Returns total items removed.
        """
        return self._cleanup_orphans()

    def _cleanup_orphans(self) -> int:
        removed = 0

        # 1. Registry entry without folder → drop entry
        for task_id in list(self._entries):
            if not self.task_folder(task_id).is_dir():
                logger.info("Removing orphan registry entry %s (no folder)", task_id)
                del self._entries[task_id]
                removed += 1

        # 2. Folder without registry entry → delete folder
        #    Scan the default tasks_dir AND any per-agent tasks_dirs from entries.
        known = set(self._entries)
        scan_dirs: set[Path] = {self._tasks_dir}
        for entry in self._entries.values():
            if entry.tasks_dir:
                scan_dirs.add(Path(entry.tasks_dir))

        for tasks_dir in scan_dirs:
            if not tasks_dir.is_dir():
                continue
            for child in tasks_dir.iterdir():
                if child.is_dir() and child.name not in known:
                    logger.info("Removing orphan task folder %s (no registry entry)", child.name)
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1

        if removed:
            self._persist()
        return removed

    def _persist(self) -> None:
        data: dict[str, Any] = {
            "tasks": [e.to_dict() for e in self._entries.values()],
        }
        atomic_json_save(self._path, data)

    def create(  # noqa: PLR0913  -- task entry fields have natural multi-arity
        self,
        submit: TaskSubmit,
        provider: str,
        model: str,
        *,
        thinking: str = "",
        tasks_dir: Path | None = None,
        priority: str = "",
    ) -> TaskEntry:
        """Create a new task entry and persist it.

        *tasks_dir* overrides the default tasks directory (for per-agent isolation).
        *priority* is coerced via ``normalise_priority`` (unknown ⇒ background).
        """
        task_id = secrets.token_hex(4)
        resolved_dir = tasks_dir or self._tasks_dir
        entry = TaskEntry(
            task_id=task_id,
            chat_id=submit.chat_id,
            parent_agent=submit.parent_agent,
            name=submit.name or task_id,
            prompt_preview=submit.prompt[:_PROMPT_PREVIEW_LEN],
            provider=provider,
            model=model,
            status="running",
            original_prompt=submit.prompt,
            thinking=thinking,
            tasks_dir=str(resolved_dir),
            thread_id=submit.thread_id,
            priority=normalise_priority(priority or submit.priority),
        )
        self._entries[task_id] = entry

        # Create task folder with TASKMEMORY.md and rule files
        folder = self.task_folder(task_id)
        folder.mkdir(parents=True, exist_ok=True)
        _seed_task_folder(folder, entry, submit.prompt, provider, model)

        self._persist()
        logger.info("Task created id=%s name='%s' provider=%s", task_id, entry.name, provider)
        return entry

    def get(self, task_id: str) -> TaskEntry | None:
        return self._entries.get(task_id)

    def find_by_name(self, chat_id: int, name: str) -> TaskEntry | None:
        """Find a task by name within a chat."""
        lower = name.lower()
        for entry in self._entries.values():
            if entry.chat_id == chat_id and entry.name.lower() == lower:
                return entry
        return None

    def list_active(self, chat_id: int | None = None) -> list[TaskEntry]:
        """Return tasks with status 'running'."""
        entries = [e for e in self._entries.values() if e.status == "running"]
        if chat_id is not None:
            entries = [e for e in entries if e.chat_id == chat_id]
        return sorted(entries, key=lambda e: e.created_at)

    def list_all(
        self,
        chat_id: int | None = None,
        parent_agent: str | None = None,
    ) -> list[TaskEntry]:
        """Return all tasks (active + completed)."""
        entries = list(self._entries.values())
        if chat_id is not None:
            entries = [e for e in entries if e.chat_id == chat_id]
        if parent_agent is not None:
            entries = [e for e in entries if e.parent_agent == parent_agent]
        return sorted(entries, key=lambda e: e.created_at, reverse=True)

    def update_status(self, task_id: str, status: str, **kwargs: object) -> None:
        """Update a task's status and optional fields."""
        entry = self._entries.get(task_id)
        if entry is None:
            return
        entry.status = status
        for key, value in kwargs.items():
            if hasattr(entry, key):
                setattr(entry, key, value)
        self._persist()

    def task_folder(self, task_id: str) -> Path:
        """Return the task's metadata folder.

        Uses the entry's stored ``tasks_dir`` when available (per-agent isolation),
        falling back to the registry-wide default.
        """
        entry = self._entries.get(task_id)
        if entry and entry.tasks_dir:
            return Path(entry.tasks_dir) / task_id
        return self._tasks_dir / task_id

    def taskmemory_path(self, task_id: str) -> Path:
        """Return the path to a task's TASKMEMORY.md."""
        return self.task_folder(task_id) / "TASKMEMORY.md"

    def cleanup_old(self, max_age_hours: int) -> int:
        """Remove completed/failed tasks older than *max_age_hours*."""
        cutoff = time.time() - max_age_hours * 3600
        to_remove: list[str] = []
        for task_id, entry in self._entries.items():
            if entry.status in _FINISHED_STATUSES and entry.created_at < cutoff:
                to_remove.append(task_id)
        return self._remove_entries(to_remove, "cleanup_old")

    def delete(self, task_id: str) -> bool:
        """Delete a single finished task (entry + folder).

        Only tasks with status done/failed/cancelled can be deleted.
        Returns ``True`` if deleted, ``False`` if not found or not deletable.
        """
        entry = self._entries.get(task_id)
        if entry is None or entry.status not in _FINISHED_STATUSES:
            return False
        self._remove_entries([task_id], "delete")
        return True

    def cleanup_finished(self, chat_id: int | None = None) -> int:
        """Remove all finished tasks (done/failed/cancelled) regardless of age."""
        to_remove: list[str] = []
        for task_id, entry in self._entries.items():
            if entry.status not in _FINISHED_STATUSES:
                continue
            if chat_id is not None and entry.chat_id != chat_id:
                continue
            to_remove.append(task_id)
        return self._remove_entries(to_remove, "cleanup_finished")

    def cleanup_finished_retention(
        self,
        *,
        max_age_hours: int,
        keep_last: int,
        now: float | None = None,
    ) -> int:
        """Prune finished task history by age and count.

        Running/waiting tasks are never removed.  ``completed_at`` is preferred
        for ordering; older entries may lack it, so fall back to ``created_at``.
        """
        if max_age_hours <= 0 and keep_last <= 0:
            return 0

        current_time = time.time() if now is None else now
        cutoff = current_time - max_age_hours * 3600 if max_age_hours > 0 else None

        finished = [
            (task_id, entry)
            for task_id, entry in self._entries.items()
            if entry.status in _FINISHED_STATUSES
        ]
        to_remove: set[str] = set()

        if cutoff is not None:
            for task_id, entry in finished:
                if _finished_sort_time(entry) < cutoff:
                    to_remove.add(task_id)

        if keep_last > 0:
            keep = {
                task_id
                for task_id, _ in sorted(
                    finished,
                    key=lambda item: _finished_sort_time(item[1]),
                    reverse=True,
                )[:keep_last]
            }
            for task_id, _ in finished:
                if task_id not in keep:
                    to_remove.add(task_id)

        return self._remove_entries(sorted(to_remove), "cleanup_finished_retention")

    def _remove_entries(self, task_ids: list[str], label: str) -> int:
        """Delete entries and their folders from the registry."""
        # Resolve folder paths before deleting entries (entries carry per-agent
        # tasks_dir overrides that task_folder() needs).
        folders = {tid: self.task_folder(tid) for tid in task_ids}
        for task_id in task_ids:
            del self._entries[task_id]
            folder = folders[task_id]
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
        if task_ids:
            self._persist()
            logger.info("%s removed %d task(s)", label, len(task_ids))
        return len(task_ids)


def _finished_sort_time(entry: TaskEntry) -> float:
    """Return the timestamp used for finished task retention."""
    return entry.completed_at or entry.created_at


# -- Task folder seeding -------------------------------------------------------

_TASK_RULES = """\
# Task Agent Rules

You are a background task agent. You have NO direct user access.

## MANDATORY: Asking Questions

If you need ANY information to complete your task (missing details,
clarifications, user preferences), you MUST use this tool:

```bash
python3 tools/task_tools/ask_parent.py "your question here"
```

This forwards your question to the parent agent and returns immediately.
Do NOT write questions in your response — the user cannot see them.
After asking, finish your current work — you will be resumed with the answer.

## Other Tools (in `tools/task_tools/`)

- `python3 tools/task_tools/list_tasks.py` — List active tasks
- `python3 tools/task_tools/cancel_task.py TASK_ID` — Cancel a task
- `python3 tools/task_tools/delete_task.py TASK_ID` — Delete a finished task

## TASKMEMORY.md

Path: `{taskmemory_path}`

Update after completing your work:
- What you did and key decisions
- Results, file paths, or findings
"""


def _seed_task_folder(
    folder: Path,
    entry: TaskEntry,
    prompt: str,
    provider: str,
    model: str,
) -> None:
    """Seed a task folder with TASKMEMORY.md and rule files."""
    taskmemory = folder / "TASKMEMORY.md"
    if not taskmemory.exists():
        taskmemory.write_text(
            f"# Task: {entry.name}\n\n"
            f"Created: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Provider: {provider}/{model}\n\n"
            f"## Task Description\n\n"
            f"{prompt[:500]}\n\n"
            f"## Progress\n\n"
            f"_Update this section as you work._\n",
            encoding="utf-8",
        )

    # Deploy rule files for all providers
    rules_content = _TASK_RULES.format(taskmemory_path=taskmemory)
    for name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
        rules_path = folder / name
        rules_path.write_text(rules_content, encoding="utf-8")
