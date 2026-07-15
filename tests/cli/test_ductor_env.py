"""Unit tests for the shared build_ductor_env helper."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.cli.ductor_env import build_ductor_env


def test_build_ductor_env_main_agent() -> None:
    env = build_ductor_env(
        agent_name="main",
        interagent_port=8799,
        transport="tg",
        chat_id=1,
        topic_id=None,
        ductor_home=Path("/home/u/.ductor"),
        shared_memory_path=Path("/home/u/.ductor/SHAREDMEMORY.md"),
    )

    assert env == {
        "DUCTOR_AGENT_NAME": "main",
        "DUCTOR_AGENT_ROLE": "main",
        "DUCTOR_INTERAGENT_PORT": "8799",
        "DUCTOR_TRANSPORT": "tg",
        "DUCTOR_HOME": "/home/u/.ductor",
        "DUCTOR_SHARED_MEMORY_PATH": "/home/u/.ductor/SHAREDMEMORY.md",
        "DUCTOR_CHAT_ID": "1",
    }


def test_build_ductor_env_sub_agent_with_topic_and_transcribe() -> None:
    env = build_ductor_env(
        agent_name="dev",
        interagent_port=9001,
        transport="mx",
        chat_id=5,
        topic_id=8,
        ductor_home=Path("/root/agents/dev"),
        shared_memory_path=Path("/root/SHAREDMEMORY.md"),
        transcribe_command="whisper.sh",
        video_transcribe_command="vid.sh",
    )

    assert env["DUCTOR_AGENT_ROLE"] == "sub"
    assert env["DUCTOR_TRANSPORT"] == "mx"
    assert env["DUCTOR_TOPIC_ID"] == "8"
    assert env["DUCTOR_TRANSCRIBE_COMMAND"] == "whisper.sh"
    assert env["DUCTOR_VIDEO_TRANSCRIBE_COMMAND"] == "vid.sh"


def test_build_ductor_env_omits_optional_when_absent() -> None:
    env = build_ductor_env(
        agent_name="main",
        interagent_port=8799,
        transport="tg",
        chat_id=0,
        topic_id=None,
        ductor_home=Path("/h"),
        shared_memory_path=Path("/h/SHAREDMEMORY.md"),
    )

    assert "DUCTOR_CHAT_ID" not in env
    assert "DUCTOR_TOPIC_ID" not in env
    assert "DUCTOR_TRANSCRIBE_COMMAND" not in env
    assert "DUCTOR_VIDEO_TRANSCRIBE_COMMAND" not in env


def test_build_ductor_env_does_not_read_os_environ(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Even with a hostile os.environ, the helper returns only what it is given.
    monkeypatch.setenv("DUCTOR_HOME", "/leaked")
    monkeypatch.setenv("SECRET", "leak")
    env = build_ductor_env(
        agent_name="main",
        interagent_port=8799,
        transport="tg",
        chat_id=1,
        topic_id=None,
        ductor_home=Path("/given"),
        shared_memory_path=Path("/given/SHAREDMEMORY.md"),
    )

    assert env["DUCTOR_HOME"] == "/given"
    assert "SECRET" not in env
