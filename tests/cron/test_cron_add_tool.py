"""Tests for the cron_add.py CLI tool (subprocess-based)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TOOL_PATH = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot"
    / "_home_defaults"
    / "workspace"
    / "tools"
    / "cron_tools"
    / "cron_add.py"
)


def _run_tool(tmp_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DUCTOR_HOME": str(tmp_path)}
    return subprocess.run(
        [sys.executable, str(TOOL_PATH), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _full_args(name: str = "test-job") -> list[str]:
    return [
        "--name",
        name,
        "--title",
        "Test Job",
        "--description",
        "A test cron job",
        "--schedule",
        "0 9 * * *",
    ]


def test_cron_add_creates_json_and_folder(tmp_path: Path) -> None:
    result = _run_tool(tmp_path, _full_args("my-job"))
    assert result.returncode == 0

    output = json.loads(result.stdout)
    assert output["job_id"] == "my-job"
    assert output["folder_created"] is True
    assert output["json_entry_created"] is True

    # JSON entry exists
    data = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert any(j["id"] == "my-job" for j in data["jobs"])

    # Folder structure exists
    task_dir = tmp_path / "workspace" / "cron_tasks" / "my-job"
    assert task_dir.is_dir()
    assert (task_dir / "CLAUDE.md").exists()
    assert (task_dir / "AGENTS.md").exists()
    assert (task_dir / "TASK_DESCRIPTION.md").exists()
    assert (task_dir / "my-job_MEMORY.md").exists()
    assert (task_dir / "scripts").is_dir()


def test_cron_add_duplicate_exits_1(tmp_path: Path) -> None:
    _run_tool(tmp_path, _full_args("dup"))
    result = _run_tool(tmp_path, _full_args("dup"))
    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert "already exists" in output["error"]


def test_cron_add_missing_params_shows_tutorial(tmp_path: Path) -> None:
    result = _run_tool(tmp_path, ["--name", "incomplete"])
    assert result.returncode == 1
    assert "CRON ADD" in result.stdout
    assert "CRON EXPRESSION FORMAT" in result.stdout
    assert "Missing required parameters" in result.stdout


def test_cron_add_no_args_shows_tutorial(tmp_path: Path) -> None:
    result = _run_tool(tmp_path, [])
    assert result.returncode == 1
    assert "CRON ADD" in result.stdout


def test_cron_add_sanitizes_name(tmp_path: Path) -> None:
    args = _full_args("My Feature!!")
    result = _run_tool(tmp_path, args)
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["job_id"] == "my-feature"


def test_cron_add_claude_md_has_fixed_content(tmp_path: Path) -> None:
    result = _run_tool(tmp_path, _full_args("rule-test"))
    assert result.returncode == 0
    task_dir = tmp_path / "workspace" / "cron_tasks" / "rule-test"
    content = (task_dir / "CLAUDE.md").read_text()
    assert "Your Mission" in content
    assert "TASK_DESCRIPTION.md" in content
    assert "automated agent" in content
    # Description should NOT be in CLAUDE.md (it's in TASK_DESCRIPTION.md)
    assert "A test cron job" not in content


def test_cron_add_creates_task_description(tmp_path: Path) -> None:
    result = _run_tool(tmp_path, _full_args("desc-test"))
    assert result.returncode == 0
    task_dir = tmp_path / "workspace" / "cron_tasks" / "desc-test"
    content = (task_dir / "TASK_DESCRIPTION.md").read_text()
    assert "A test cron job" in content
    assert "Test Job" in content
    assert "## Assignment" in content
    assert "## Output" in content


def test_cron_add_json_has_fixed_instruction(tmp_path: Path) -> None:
    result = _run_tool(tmp_path, _full_args("instr-test"))
    assert result.returncode == 0
    data = json.loads((tmp_path / "cron_jobs.json").read_text())
    job = next(j for j in data["jobs"] if j["id"] == "instr-test")
    assert "TASK_DESCRIPTION.md" in job["agent_instruction"]


def test_cron_add_output_includes_action_required(tmp_path: Path) -> None:
    result = _run_tool(tmp_path, _full_args("step-test"))
    assert result.returncode == 0
    output = json.loads(result.stdout)
    actions = output["action_required"]
    assert isinstance(actions, list)
    assert len(actions) >= 3
    joined = " ".join(actions)
    assert "TASK_DESCRIPTION.md" in joined
    assert "scripts/" in joined
    assert "step-test_MEMORY.md" in joined


def test_cron_add_agents_md_mirrors_claude_md(tmp_path: Path) -> None:
    result = _run_tool(tmp_path, _full_args("mirror-test"))
    assert result.returncode == 0
    task_dir = tmp_path / "workspace" / "cron_tasks" / "mirror-test"
    assert (task_dir / "CLAUDE.md").read_text() == (task_dir / "AGENTS.md").read_text()


def test_cron_add_no_venv_by_default(tmp_path: Path) -> None:
    result = _run_tool(tmp_path, _full_args("venv-test"))
    assert result.returncode == 0
    task_dir = tmp_path / "workspace" / "cron_tasks" / "venv-test"
    assert not (task_dir / ".venv").exists()
