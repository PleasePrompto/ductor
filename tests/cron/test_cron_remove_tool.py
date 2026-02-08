"""Tests for the cron_remove.py CLI tool (subprocess-based)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TOOL_ADD = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot"
    / "_home_defaults"
    / "workspace"
    / "tools"
    / "cron_tools"
    / "cron_add.py"
)
TOOL_REMOVE = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot"
    / "_home_defaults"
    / "workspace"
    / "tools"
    / "cron_tools"
    / "cron_remove.py"
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


def _add_job(tmp_path: Path, name: str = "rm-test") -> None:
    result = _run(
        tmp_path,
        TOOL_ADD,
        [
            "--name",
            name,
            "--title",
            "Remove Test",
            "--description",
            "For removal testing",
            "--schedule",
            "0 9 * * *",
        ],
    )
    assert result.returncode == 0


def test_cron_remove_deletes_json_and_folder(tmp_path: Path) -> None:
    _add_job(tmp_path, "to-delete")
    task_dir = tmp_path / "workspace" / "cron_tasks" / "to-delete"
    assert task_dir.is_dir()

    result = _run(tmp_path, TOOL_REMOVE, ["to-delete"])
    assert result.returncode == 0

    output = json.loads(result.stdout)
    assert output["json_entry_removed"] is True
    assert output["folder_deleted"] is True

    # JSON entry gone
    data = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert not any(j["id"] == "to-delete" for j in data["jobs"])

    # Folder gone
    assert not task_dir.exists()


def test_cron_remove_nonexistent_exits_1(tmp_path: Path) -> None:
    # Create an empty jobs file
    (tmp_path / "cron_jobs.json").write_text('{"jobs": []}')
    result = _run(tmp_path, TOOL_REMOVE, ["ghost"])
    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert "not found" in output["error"]


def test_cron_remove_no_args_shows_tutorial(tmp_path: Path) -> None:
    result = _run(tmp_path, TOOL_REMOVE, [])
    assert result.returncode == 1
    assert "CRON REMOVE" in result.stdout


def test_cron_remove_handles_missing_folder(tmp_path: Path) -> None:
    """If the folder was already deleted, remove still removes the JSON entry."""
    _add_job(tmp_path, "orphan-json")
    # Manually delete the folder
    task_dir = tmp_path / "workspace" / "cron_tasks" / "orphan-json"
    shutil.rmtree(task_dir)

    result = _run(tmp_path, TOOL_REMOVE, ["orphan-json"])
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["json_entry_removed"] is True
    assert output["folder_deleted"] is False
