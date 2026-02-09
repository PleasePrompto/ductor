"""Tests for cron task template rendering (moved from templates.py to cron_tasks.py)."""

from __future__ import annotations

from ductor_bot.workspace.cron_tasks import render_cron_task_claude_md, render_task_description_md


def test_render_cron_task_claude_md_contains_name() -> None:
    result = render_cron_task_claude_md("my-feature")
    assert "my-feature" in result


def test_render_cron_task_claude_md_references_task_description() -> None:
    result = render_cron_task_claude_md("my-feature")
    assert "TASK_DESCRIPTION.md" in result


def test_render_cron_task_claude_md_has_memory_reference() -> None:
    result = render_cron_task_claude_md("my-feature")
    assert "my-feature_MEMORY.md" in result


def test_render_cron_task_claude_md_is_markdown() -> None:
    result = render_cron_task_claude_md("test")
    assert result.startswith("#")


def test_render_cron_task_claude_md_has_fixed_rules() -> None:
    result = render_cron_task_claude_md("my-feature")
    assert "scripts" in result.lower()
    assert ".venv" in result


def test_render_task_description_md_contains_title() -> None:
    result = render_task_description_md("Daily Weather", "Check weather in Muenster")
    assert "Daily Weather" in result


def test_render_task_description_md_contains_description() -> None:
    result = render_task_description_md("Daily Weather", "Check weather in Muenster")
    assert "Check weather in Muenster" in result


def test_render_task_description_md_has_sections() -> None:
    result = render_task_description_md("Test", "A test task")
    assert "## Goal" in result
    assert "## Assignment" in result
    assert "## Output" in result


def test_render_task_description_md_is_markdown() -> None:
    result = render_task_description_md("Test", "desc")
    assert result.startswith("#")
