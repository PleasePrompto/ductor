"""Dropping the CLI to another unix account.

The Consult topic's isolation is a rule the agent is asked to follow. This is
the part the kernel enforces: the account it runs as cannot read the project
tree, whatever the agent decides to do.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ductor_bot.cli.base import CLIConfig, run_as_wrap
from ductor_bot.cli.service import CLIService
from ductor_bot.workspace.paths import CONSULT_USER

ENV = {
    "PATH": "/opt/npm-global/bin:/usr/bin",
    "HOME": "/home/ductor",
    "CLAUDE_CONFIG_DIR": "/home/ductor/.claude",
    "SECRET": "should-not-travel",
}


def test_no_user_means_no_wrapper() -> None:
    cmd = ["claude", "-p"]
    assert run_as_wrap(cmd, CLIConfig(), ENV) == cmd


def test_the_command_is_dropped_to_the_account() -> None:
    wrapped = run_as_wrap(["claude", "-p"], CLIConfig(run_as_user="consult"), ENV)
    assert wrapped[:4] == ["sudo", "-n", "-u", "consult"]
    assert wrapped[-2:] == ["claude", "-p"]


def test_path_is_restated_because_sudo_replaces_it() -> None:
    """secure_path does not contain the npm prefix the CLIs live under."""
    wrapped = run_as_wrap(["claude"], CLIConfig(run_as_user="consult"), ENV)
    assert f"PATH={ENV['PATH']}" in wrapped


def test_home_points_at_the_new_account() -> None:
    """Not the bot's home: that is the directory being kept away."""
    wrapped = run_as_wrap(["claude"], CLIConfig(run_as_user="consult"), ENV)
    assert "HOME=/home/consult" in wrapped
    assert "HOME=/home/ductor" not in wrapped


def test_unrelated_environment_is_not_carried_over() -> None:
    wrapped = run_as_wrap(["claude"], CLIConfig(run_as_user="consult"), ENV)
    assert not any("SECRET" in part for part in wrapped)


def test_sudo_does_not_prompt() -> None:
    """A prompt would hang the subprocess forever rather than fail."""
    wrapped = run_as_wrap(["claude"], CLIConfig(run_as_user="consult"), ENV)
    assert "-n" in wrapped


# ---------------------------------------------------------------------------
# When the service decides to drop
# ---------------------------------------------------------------------------


def _service(workspace: Path) -> CLIService:
    service = object.__new__(CLIService)
    service._config = SimpleNamespace(working_dir=str(workspace), docker_container=None)
    service._working_dir_resolver = None
    return service


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".ductor" / "workspace"
    ws.mkdir(parents=True)
    (tmp_path / ".ductor" / "Consult").mkdir()
    return ws


def test_the_consult_directory_drops(workspace: Path) -> None:
    consult = workspace.parent / "Consult"
    assert _service(workspace)._is_consult_dir(str(consult)) is True


def test_a_project_directory_does_not(workspace: Path, tmp_path: Path) -> None:
    project = tmp_path / "IT" / "EMR"
    project.mkdir(parents=True)
    assert _service(workspace)._is_consult_dir(str(project)) is False


def test_the_workspace_itself_does_not(workspace: Path) -> None:
    assert _service(workspace)._is_consult_dir(str(workspace)) is False


def test_a_lookalike_path_does_not(workspace: Path, tmp_path: Path) -> None:
    """Matching by name rather than by resolved path would be a way in."""
    decoy = tmp_path / "Consult"
    decoy.mkdir()
    assert _service(workspace)._is_consult_dir(str(decoy)) is False


def test_the_account_name_is_shared_not_duplicated() -> None:
    """Transport and CLI layers must agree, without importing each other."""
    from ductor_bot.messenger.telegram import managed_topics

    assert managed_topics.CONSULT_USER == CONSULT_USER == "consult"


def test_the_bots_config_dir_does_not_follow_the_drop() -> None:
    """It lives in the bot's home, which the account cannot read. Passing it
    would send the CLI looking somewhere it has no access to; unset, it falls
    back to the account's own $HOME."""
    wrapped = run_as_wrap(["claude"], CLIConfig(run_as_user="consult"), ENV)
    assert not any(part.startswith("CLAUDE_CONFIG_DIR=") for part in wrapped)
    assert not any("CLAUDE_SECURESTORAGE_CONFIG_DIR=" in part for part in wrapped)
