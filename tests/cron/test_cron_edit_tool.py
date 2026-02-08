"""Tests for the cron_edit.py CLI tool (subprocess-based)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

TOOL_ADD = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot"
    / "_home_defaults"
    / "workspace"
    / "tools"
    / "cron_tools"
    / "cron_add.py"
)
TOOL_EDIT = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot"
    / "_home_defaults"
    / "workspace"
    / "tools"
    / "cron_tools"
    / "cron_edit.py"
)


def _run(tmp_path: Path, tool: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DUCTOR_HOME": str(tmp_path)}
    return subprocess.run(
        [sys.executable, str(tool), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _add_job(tmp_path: Path, name: str = "edit-test") -> None:
    result = _run(
        tmp_path,
        TOOL_ADD,
        [
            "--name",
            name,
            "--title",
            "Edit Test",
            "--description",
            "Original description",
            "--schedule",
            "0 9 * * *",
        ],
    )
    assert result.returncode == 0


def _job(tmp_path: Path, job_id: str) -> dict[str, Any]:
    data = json.loads((tmp_path / "cron_jobs.json").read_text())
    return next(j for j in data["jobs"] if j["id"] == job_id)


def test_cron_edit_updates_title_description_schedule(tmp_path: Path) -> None:
    _add_job(tmp_path, "meta-job")

    result = _run(
        tmp_path,
        TOOL_EDIT,
        [
            "meta-job",
            "--title",
            "Meta Job Updated",
            "--description",
            "New description",
            "--schedule",
            "30 7 * * 1-5",
        ],
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["updated"] is True
    assert "title" in output["updated_fields"]
    assert "description" in output["updated_fields"]
    assert "schedule" in output["updated_fields"]

    job = _job(tmp_path, "meta-job")
    assert job["title"] == "Meta Job Updated"
    assert job["description"] == "New description"
    assert job["schedule"] == "30 7 * * 1-5"


def test_cron_edit_rename_updates_json_and_folder(tmp_path: Path) -> None:
    _add_job(tmp_path, "old-name")
    old_dir = tmp_path / "workspace" / "cron_tasks" / "old-name"
    assert (old_dir / "old-name_MEMORY.md").exists()

    result = _run(tmp_path, TOOL_EDIT, ["old-name", "--name", "new-name"])
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["job_id"] == "new-name"
    assert output["folder_renamed"] is True
    assert output["memory_file_renamed"] is True
    assert "id" in output["updated_fields"]
    assert "task_folder" in output["updated_fields"]

    data = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert any(j["id"] == "new-name" and j["task_folder"] == "new-name" for j in data["jobs"])
    assert not old_dir.exists()

    new_dir = tmp_path / "workspace" / "cron_tasks" / "new-name"
    assert new_dir.is_dir()
    assert (new_dir / "new-name_MEMORY.md").exists()
    claude = (new_dir / "CLAUDE.md").read_text()
    agents = (new_dir / "AGENTS.md").read_text()
    assert "new-name_MEMORY.md" in claude
    assert agents == claude


def test_cron_edit_disable_then_enable(tmp_path: Path) -> None:
    _add_job(tmp_path, "toggle-job")

    disabled = _run(tmp_path, TOOL_EDIT, ["toggle-job", "--disable"])
    assert disabled.returncode == 0
    assert _job(tmp_path, "toggle-job")["enabled"] is False

    enabled = _run(tmp_path, TOOL_EDIT, ["toggle-job", "--enable"])
    assert enabled.returncode == 0
    assert _job(tmp_path, "toggle-job")["enabled"] is True


def test_cron_edit_no_change_flags_exits_1(tmp_path: Path) -> None:
    _add_job(tmp_path, "no-change")
    result = _run(tmp_path, TOOL_EDIT, ["no-change"])
    assert result.returncode == 1
    assert "CRON EDIT" in result.stdout
    assert "Missing changes" in result.stdout


def test_cron_edit_nonexistent_exits_1(tmp_path: Path) -> None:
    (tmp_path / "cron_jobs.json").write_text('{"jobs": []}')
    result = _run(tmp_path, TOOL_EDIT, ["ghost", "--title", "x"])
    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert "not found" in output["error"]
