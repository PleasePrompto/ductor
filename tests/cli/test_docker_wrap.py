"""Tests for cli/base.py: docker_wrap helper."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.cli.base import docker_wrap


def test_docker_wrap_without_container() -> None:
    cmd = ["claude", "-p", "hello"]
    result_cmd, cwd = docker_wrap(cmd, "", 123, Path("/workspace"))
    assert result_cmd == cmd
    assert cwd == str(Path("/workspace"))


def test_docker_wrap_with_container() -> None:
    cmd = ["claude", "-p", "hello"]
    result_cmd, cwd = docker_wrap(cmd, "my-sandbox", 42, Path("/workspace"))
    assert result_cmd == [
        "docker",
        "exec",
        "-e",
        "DUCTOR_CHAT_ID=42",
        "my-sandbox",
        "claude",
        "-p",
        "hello",
    ]
    assert cwd is None


def test_docker_wrap_preserves_full_command() -> None:
    cmd = ["claude", "-p", "test", "--model", "opus", "--verbose"]
    result_cmd, _ = docker_wrap(cmd, "sandbox", 1, Path("/w"))
    assert result_cmd[-6:] == cmd


def test_docker_wrap_injects_chat_id() -> None:
    cmd = ["codex", "exec"]
    result_cmd, _ = docker_wrap(cmd, "box", 999, Path("/w"))
    assert "DUCTOR_CHAT_ID=999" in result_cmd
