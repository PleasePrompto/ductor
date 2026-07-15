from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from ductor_bot.cli.interactive.repl_pool import (
    ReplPool,
    ReplPoolConfig,
    ReplTimeoutError,
    boot_actions_for_screen,
    extract_reply_after_nonce,
    fatal_error_for_screen,
    session_name,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class FakeRunner:
    def __init__(self, *, screens: list[str] | None = None) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.sessions: set[str] = set()
        self.screens = screens or []

    def __call__(
        self, args: Sequence[str], input_text: str | None
    ) -> subprocess.CompletedProcess[str]:
        raw_argv = list(args)
        self.calls.append((raw_argv, input_text))
        argv = self._unwrap_docker(raw_argv)
        if argv[:2] == ["tmux", "new-session"]:
            self.sessions.add(argv[argv.index("-s") + 1])
            return subprocess.CompletedProcess(raw_argv, 0, "", "")
        if argv[:2] == ["tmux", "has-session"]:
            name = argv[argv.index("-t") + 1]
            return subprocess.CompletedProcess(
                raw_argv, 0 if name in self.sessions else 1, "", "missing"
            )
        if argv[:2] == ["tmux", "kill-session"]:
            self.sessions.discard(argv[argv.index("-t") + 1])
            return subprocess.CompletedProcess(raw_argv, 0, "", "")
        if argv[:2] == ["tmux", "capture-pane"]:
            screen = self.screens.pop(0) if self.screens else ""
            return subprocess.CompletedProcess(raw_argv, 0, screen, "")
        if argv[:2] == ["tmux", "ls"]:
            stdout = "".join(f"{name}: 1 windows\n" for name in sorted(self.sessions))
            return subprocess.CompletedProcess(raw_argv, 0, stdout, "")
        return subprocess.CompletedProcess(raw_argv, 0, "", "")

    def _unwrap_docker(self, argv: list[str]) -> list[str]:
        if argv[:2] != ["docker", "exec"]:
            return argv
        offset = 4 if len(argv) > 2 and argv[2] == "-i" else 3
        return argv[offset:]

    def count(self, *prefix: str) -> int:
        return sum(1 for args, _input in self.calls if tuple(args[: len(prefix)]) == prefix)

    def has_call(self, *parts: str) -> bool:
        return any(all(part in args for part in parts) for args, _input in self.calls)


def _config(tmp_path: Path, **overrides: object) -> ReplPoolConfig:
    values = {
        "agent": "main",
        "working_dir": tmp_path,
        "claude_home": tmp_path / "claude",
        "signal_dir": tmp_path / "signals",
        "boot_timeout_seconds": 0.0,
        "signal_timeout_seconds": 1.0,
        "idle_ttl_seconds": 60.0,
        "max_sessions": 4,
    }
    values.update(overrides)
    return ReplPoolConfig(**values)  # type: ignore[arg-type]


def _write_transcript(claude_home: Path, session_id: str, nonce: str, text: str) -> Path:
    path = claude_home / "projects" / "hash" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "user", "message": {"content": f"hello [[reply-token:{nonce}]]"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "result": text},
    ]
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


def _touch_signal(signal_dir: Path, agent: str, session_id: str, nonce: str) -> None:
    path = signal_dir / agent / f"{session_id}.{nonce}.done"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def test_session_name_format_and_sanitization() -> None:
    assert session_name("dev.agent", "tg", 123, None) == "ductor-repl-dev.agent-tg-123-none"
    assert session_name("main", "tg", "chat:1", "topic/2") == "ductor-repl-main-tg-chat-1-topic-2"


def test_boot_prompt_state_machine() -> None:
    trust_screen = "Is this a project you trust?\n> 1. Yes, I trust this folder\n  2. No, exit\n"
    bypass_screen = "Bypass Permissions mode\n> 1. No, exit\n  2. Yes, I accept\n"

    assert boot_actions_for_screen(trust_screen) == ("Enter",)
    assert boot_actions_for_screen(bypass_screen) == ("Down", "Enter")
    assert boot_actions_for_screen("Claude is ready") == ()


def test_boot_prompt_multi_key_actions_are_delayed(tmp_path: Path) -> None:
    bypass_screen = "Bypass Permissions mode\n> 1. No, exit\n  2. Yes, I accept\n"
    clock = FakeClock()
    runner = FakeRunner(screens=[bypass_screen, ""])
    pool = ReplPool(
        _config(tmp_path, boot_timeout_seconds=2.0, boot_key_delay=0.4),
        runner=runner,
        now=clock.now,
        sleep=clock.sleep,
    )

    pool._handle_boot_prompts("boot-session")

    keys = [args[-1] for args, _input in runner.calls if args[:2] == ["tmux", "send-keys"]]
    assert keys == ["Down", "Enter"]
    assert clock.sleeps[0] == 0.4


def test_fatal_error_for_screen_detects_rejected_model() -> None:
    err = (
        "There's an issue with the selected model (claude-fable-5). "
        "It may not exist or you may not have access to it. Run --model to pick another."
    )
    assert fatal_error_for_screen(err) is not None
    assert fatal_error_for_screen("Welcome to Claude Code\n> ") is None
    assert fatal_error_for_screen("Do you trust the files in this folder?") is None


def test_fatal_error_for_screen_detects_auth_login_failure() -> None:
    screen = "Please run /login · API Error: 401 Invalid authentication credentials"
    assert fatal_error_for_screen(screen) is not None
    assert fatal_error_for_screen("Welcome to Claude Code\n> ") is None
    # The 401 token is bound to the auth phrase: a bare "API Error: 401" in
    # normal conversation/output must NOT trip the fatal detector.
    assert (
        fatal_error_for_screen("Sure, an API Error: 401 means the request was unauthorized.")
        is None
    )


def test_idle_evicted_session_respawns_with_resume(tmp_path: Path) -> None:
    """Regression: a seeded/idle session that gets idle-evicted on the next turn
    must still respawn with --resume <old_sid>, not start a fresh session,
    which would lose the prior conversation."""
    clock = FakeClock()
    runner = FakeRunner()
    pool = ReplPool(
        _config(tmp_path, idle_ttl_seconds=10.0),
        runner=runner,
        now=clock.now,
        sleep=clock.sleep,
    )
    pool.seed_resume_session(
        transport="tg", chat=1, topic=None, model="opus", system_prompt="", session_id="old-sid-123"
    )
    # Let the seeded entry go idle past the TTL, then spawn the next turn.
    clock.value += 100.0
    pool._get_or_spawn(("tg", str(1), "none"), "tg", 1, None, "opus", "")

    spawn_cmds = [" ".join(args) for args, _ in runner.calls if args[:2] == ["tmux", "new-session"]]
    assert spawn_cmds, "expected a tmux new-session spawn"
    assert any("--resume old-sid-123" in cmd for cmd in spawn_cmds), spawn_cmds


def test_boot_does_not_abort_on_stale_resumed_error(tmp_path: Path) -> None:
    """--resume redraws past conversation; an old model error in the pane must
    NOT abort boot (regression: false-positive broke valid resumed turns)."""
    stale = (
        "There's an issue with the selected model (claude-fable-5). "
        "It may not exist or you may not have access to it."
    )
    clock = FakeClock()
    runner = FakeRunner(screens=[stale, ""])
    pool = ReplPool(
        _config(tmp_path, boot_timeout_seconds=2.0),
        runner=runner,
        now=clock.now,
        sleep=clock.sleep,
    )
    # Must not raise — boot no longer scans for fatal markers.
    pool._handle_boot_prompts("boot-session")


def test_boot_prompt_single_key_action_has_no_key_delay(tmp_path: Path) -> None:
    trust_screen = "Is this a project you trust?\n> 1. Yes, I trust this folder\n  2. No, exit\n"
    clock = FakeClock()
    runner = FakeRunner(screens=[trust_screen, ""])
    pool = ReplPool(
        _config(tmp_path, boot_timeout_seconds=2.0, boot_key_delay=0.4),
        runner=runner,
        now=clock.now,
        sleep=clock.sleep,
    )

    pool._handle_boot_prompts("boot-session")

    keys = [args[-1] for args, _input in runner.calls if args[:2] == ["tmux", "send-keys"]]
    assert keys == ["Enter"]
    assert 0.4 not in clock.sleeps


def test_extract_reply_after_nonce_uses_user_boundary(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    old = {"type": "assistant", "message": {"content": "old"}}
    events = [
        old,
        {"type": "user", "message": {"content": "prompt [[reply-token:abc123]]"}},
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "part 1 "}, {"type": "text", "text": "part 2"}]
            },
        },
        {"type": "result", "result": "ignored boundary"},
        {"type": "user", "message": {"content": "next"}},
        {"type": "assistant", "message": {"content": "too late"}},
    ]
    transcript.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    offset = len(json.dumps(old) + "\n")

    assert (
        extract_reply_after_nonce(transcript, nonce="abc123", start_offset=offset)
        == "part 1 part 2"
    )


def test_extract_reply_after_nonce_skips_tool_result_user_events(tmp_path: Path) -> None:
    """Tool results are user-role events; they must not truncate extraction."""
    transcript = tmp_path / "session.jsonl"
    events = [
        {
            "type": "user",
            "message": {
                "content": [{"type": "text", "text": "[[reply-token:abc123]] count my pings"}]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "..."},
                    {"type": "tool_use", "name": "Bash"},
                ]
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "log lines"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "five pings"}]}},
        {"type": "attachment"},
        {"type": "system"},
    ]
    transcript.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    assert extract_reply_after_nonce(transcript, nonce="abc123") == "five pings"


def test_signal_timeout_returns_failure(tmp_path: Path) -> None:
    clock = FakeClock()
    runner = FakeRunner()
    pool = ReplPool(
        _config(tmp_path, signal_timeout_seconds=0.2),
        runner=runner,
        now=clock.now,
        sleep=clock.sleep,
        token_factory=lambda: "deadbeef",
    )

    with pytest.raises(ReplTimeoutError):
        pool.send(
            transport="tg", chat=1, topic=None, model="sonnet", system_prompt="sys", prompt="hello"
        )


def test_pool_is_lazy_and_reuses_existing_session(tmp_path: Path) -> None:
    runner = FakeRunner()
    claude_home = tmp_path / "claude"
    signal_dir = tmp_path / "signals"
    _write_transcript(claude_home, "sess-1", "aaa111", "answer")
    _touch_signal(signal_dir, "main", "sess-1", "aaa111")
    pool = ReplPool(
        _config(tmp_path, claude_home=claude_home, signal_dir=signal_dir),
        runner=runner,
        token_factory=lambda: "aaa111",
    )

    assert runner.count("tmux", "new-session") == 0
    assert pool.send(
        transport="tg", chat=1, topic=None, model="sonnet", system_prompt="sys", prompt="hello"
    ) == (
        "answer",
        "sess-1",
    )
    _touch_signal(signal_dir, "main", "sess-1", "aaa111")
    assert pool.send(
        transport="tg", chat=1, topic=None, model="sonnet", system_prompt="sys", prompt="hello"
    ) == (
        "answer",
        "sess-1",
    )
    assert runner.count("tmux", "new-session") == 1
    assert runner.count("tmux", "has-session") == 1


def test_pool_idle_evicts_and_cap_evicts_lru(tmp_path: Path) -> None:
    clock = FakeClock()
    runner = FakeRunner()
    claude_home = tmp_path / "claude"
    signal_dir = tmp_path / "signals"
    tokens = iter(["n1", "n2", "n3"])
    for session_id, nonce, text in [
        ("s1", "n1", "one"),
        ("s2", "n2", "two"),
        ("s3", "n3", "three"),
    ]:
        _write_transcript(claude_home, session_id, nonce, text)
        _touch_signal(signal_dir, "main", session_id, nonce)

    pool = ReplPool(
        _config(
            tmp_path,
            claude_home=claude_home,
            signal_dir=signal_dir,
            idle_ttl_seconds=1.0,
            max_sessions=1,
        ),
        runner=runner,
        now=clock.now,
        sleep=clock.sleep,
        token_factory=lambda: next(tokens),
    )

    assert pool.send(
        transport="tg", chat=1, topic=None, model="sonnet", system_prompt="sys", prompt="one"
    ) == (
        "one",
        "s1",
    )
    clock.sleep(2.0)
    assert pool.send(
        transport="tg", chat=2, topic=None, model="sonnet", system_prompt="sys", prompt="two"
    ) == (
        "two",
        "s2",
    )
    assert runner.has_call("kill-session", "ductor-repl-main-tg-1-none")

    assert pool.send(
        transport="tg", chat=3, topic=None, model="sonnet", system_prompt="sys", prompt="three"
    ) == (
        "three",
        "s3",
    )
    assert runner.has_call("kill-session", "ductor-repl-main-tg-2-none")


def test_kill_is_exact_chat_topic_match(tmp_path: Path) -> None:
    runner = FakeRunner()
    pool = ReplPool(_config(tmp_path), runner=runner)
    runner.sessions.update(
        {
            "ductor-repl-main-tg-1-10",
            "ductor-repl-main-tg-1-20",
        }
    )

    assert pool.kill(transport="tg", chat=1, topic=10) == 1

    assert "ductor-repl-main-tg-1-10" not in runner.sessions
    assert "ductor-repl-main-tg-1-20" in runner.sessions


def test_claude_command_uses_env_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/allowed/bin")
    monkeypatch.setenv("HOME", "/allowed/home")
    monkeypatch.setenv("SECRET_TOKEN", "do-not-leak")
    pool = ReplPool(_config(tmp_path, env_allowlist=("PATH", "HOME")))

    command = pool._claude_command("sonnet", "sys", None, "tg", "1", "none")
    argv = shlex.split(command)

    assert argv[:2] == ["env", "-i"]
    assert "PATH=/allowed/bin" in argv
    assert "HOME=/allowed/home" in argv
    assert f"CLAUDE_CONFIG_DIR={tmp_path / 'claude'}" in argv
    assert "SECRET_TOKEN=do-not-leak" not in argv


def test_claude_command_injects_chat_and_topic_ids(tmp_path: Path) -> None:
    pool = ReplPool(_config(tmp_path))

    argv = shlex.split(pool._claude_command("sonnet", "sys", None, "tg", "-1009999999999", "8"))

    assert "DUCTOR_CHAT_ID=-1009999999999" in argv
    assert "DUCTOR_TOPIC_ID=8" in argv


def test_claude_command_omits_topic_id_for_dm(tmp_path: Path) -> None:
    pool = ReplPool(_config(tmp_path))

    argv = shlex.split(pool._claude_command("sonnet", "sys", None, "tg", "-1009999999999", "none"))

    assert "DUCTOR_CHAT_ID=-1009999999999" in argv
    assert not any(part.startswith("DUCTOR_TOPIC_ID=") for part in argv)


def test_dockerfiles_install_tmux() -> None:
    assert "tmux" in Path("Dockerfile.sandbox").read_text(encoding="utf-8")


def test_claude_command_includes_effort(tmp_path: Path) -> None:
    pool = ReplPool(_config(tmp_path))
    argv = shlex.split(pool._claude_command("sonnet", "sys", None, "tg", "1", "none", "high"))
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "high"


def test_claude_command_omits_effort_when_unset(tmp_path: Path) -> None:
    pool = ReplPool(_config(tmp_path))
    argv = shlex.split(pool._claude_command("sonnet", "sys", None, "tg", "1", "none", ""))
    assert "--effort" not in argv


def test_claude_command_omits_effort_when_default(tmp_path: Path) -> None:
    pool = ReplPool(_config(tmp_path))
    argv = shlex.split(pool._claude_command("sonnet", "sys", None, "tg", "1", "none", "default"))
    assert "--effort" not in argv


def test_spawn_threads_effort_into_argv(tmp_path: Path) -> None:
    runner = FakeRunner()
    pool = ReplPool(_config(tmp_path), runner=runner)
    pool.spawn(
        agent="main",
        transport="tg",
        chat=1,
        topic=None,
        model="sonnet",
        system_prompt="sys",
        effort="max",
    )
    new_session = next(args for args, _input in runner.calls if "new-session" in args)
    command = new_session[-1]
    assert "--effort max" in command


def test_send_threads_effort_into_spawn_argv(tmp_path: Path) -> None:
    runner = FakeRunner()
    claude_home = tmp_path / "claude"
    signal_dir = tmp_path / "signals"
    _write_transcript(claude_home, "sess-1", "aaa111", "answer")
    _touch_signal(signal_dir, "main", "sess-1", "aaa111")
    pool = ReplPool(
        _config(tmp_path, claude_home=claude_home, signal_dir=signal_dir),
        runner=runner,
        token_factory=lambda: "aaa111",
    )

    pool.send(
        transport="tg",
        chat=1,
        topic=None,
        model="sonnet",
        system_prompt="sys",
        prompt="hello",
        effort="high",
    )

    new_session = next(args for args, _input in runner.calls if "new-session" in args)
    assert "--effort high" in new_session[-1]


def test_docker_container_wraps_tmux_commands_and_uses_container_paths(tmp_path: Path) -> None:
    runner = FakeRunner()
    pool = ReplPool(
        _config(
            tmp_path,
            docker_container="ductor-sandbox",
            container_working_dir=Path("/ductor/workspace"),
            container_claude_home=Path("/ductor/.claude"),
        ),
        runner=runner,
    )

    pool.spawn(agent="main", transport="tg", chat=1, topic=2, model="sonnet", system_prompt="sys")

    new_session = next(args for args, _input in runner.calls if "new-session" in args)
    assert new_session[:4] == ["docker", "exec", "ductor-sandbox", "tmux"]
    assert new_session[new_session.index("-c") + 1] == "/ductor/workspace"
    command = new_session[-1]
    assert "CLAUDE_CONFIG_DIR=/ductor/.claude" in command


def test_claude_command_respects_permission_and_tool_denylist(tmp_path: Path) -> None:
    pool = ReplPool(_config(tmp_path, permission_mode="plan", tool_denylist=("Bash", "Write")))

    argv = shlex.split(pool._claude_command("sonnet", "sys", None, "tg", "1", "none"))

    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--disallowedTools") + 1 : argv.index("--disallowedTools") + 3] == [
        "Bash",
        "Write",
    ]


def test_send_unlinks_signal_and_deletes_paste_buffer(tmp_path: Path) -> None:
    runner = FakeRunner()
    claude_home = tmp_path / "claude"
    signal_dir = tmp_path / "signals"
    _write_transcript(claude_home, "sess-1", "aaa111", "answer")
    _touch_signal(signal_dir, "main", "sess-1", "aaa111")
    pool = ReplPool(
        _config(tmp_path, claude_home=claude_home, signal_dir=signal_dir, paste_threshold=1),
        runner=runner,
        token_factory=lambda: "aaa111",
    )

    assert pool.send(
        transport="tg", chat=1, topic=None, model="sonnet", system_prompt="sys", prompt="hello"
    ) == (
        "answer",
        "sess-1",
    )

    assert not (signal_dir / "main" / "sess-1.aaa111.done").exists()
    assert runner.has_call("delete-buffer")


def test_claude_env_includes_ductor_vars_without_os_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/allowed/bin")
    monkeypatch.setenv("SECRET_TOKEN", "do-not-leak")
    pool = ReplPool(
        _config(
            tmp_path,
            agent="main",
            ductor_home=tmp_path / "home",
            shared_memory_path=tmp_path / "home" / "SHAREDMEMORY.md",
            interagent_port=9001,
            transcribe_command="whisper.sh",
            env_allowlist=("PATH",),
        )
    )

    env = pool._claude_env("tg", "1", "8")

    assert env["DUCTOR_AGENT_NAME"] == "main"
    assert env["DUCTOR_AGENT_ROLE"] == "main"
    assert env["DUCTOR_INTERAGENT_PORT"] == "9001"
    assert env["DUCTOR_TRANSPORT"] == "tg"
    assert env["DUCTOR_HOME"] == str(tmp_path / "home")
    assert env["DUCTOR_SHARED_MEMORY_PATH"] == str(tmp_path / "home" / "SHAREDMEMORY.md")
    assert env["DUCTOR_CHAT_ID"] == "1"
    assert env["DUCTOR_TOPIC_ID"] == "8"
    assert env["DUCTOR_TRANSCRIBE_COMMAND"] == "whisper.sh"
    # env -i model intact: non-allowlisted host vars must not leak in.
    assert "SECRET_TOKEN" not in env
    assert "DUCTOR_INTERAGENT_HOST" not in env  # host-mode


def test_claude_env_docker_mode_uses_container_paths_and_interagent_host(tmp_path: Path) -> None:
    pool = ReplPool(
        _config(
            tmp_path,
            docker_container="ductor-sandbox",
            ductor_home=Path("/ductor"),
            shared_memory_path=Path("/ductor/SHAREDMEMORY.md"),
            container_claude_home=Path("/ductor/.claude"),
        )
    )

    env = pool._claude_env("tg", "1", "none")

    assert env["DUCTOR_HOME"] == "/ductor"
    assert env["DUCTOR_SHARED_MEMORY_PATH"] == "/ductor/SHAREDMEMORY.md"
    assert env["DUCTOR_INTERAGENT_HOST"] == "host.docker.internal"


def test_claude_env_transport_is_per_request(tmp_path: Path) -> None:
    pool = ReplPool(_config(tmp_path, ductor_home=tmp_path, shared_memory_path=tmp_path / "s.md"))

    assert pool._claude_env("tg", "1", "none")["DUCTOR_TRANSPORT"] == "tg"
    assert pool._claude_env("mx", "1", "none")["DUCTOR_TRANSPORT"] == "mx"


def test_same_chat_topic_different_transport_are_distinct_sessions(tmp_path: Path) -> None:
    runner = FakeRunner()
    pool = ReplPool(_config(tmp_path), runner=runner)

    pool.spawn(agent="main", transport="tg", chat=1, topic=None, model="sonnet", system_prompt="s")
    pool.spawn(agent="main", transport="mx", chat=1, topic=None, model="sonnet", system_prompt="s")

    assert session_name("main", "tg", 1, None) == "ductor-repl-main-tg-1-none"
    assert session_name("main", "mx", 1, None) == "ductor-repl-main-mx-1-none"
    assert "ductor-repl-main-tg-1-none" in runner.sessions
    assert "ductor-repl-main-mx-1-none" in runner.sessions


def test_kill_only_targets_matching_transport(tmp_path: Path) -> None:
    runner = FakeRunner()
    pool = ReplPool(_config(tmp_path), runner=runner)
    runner.sessions.update({"ductor-repl-main-tg-1-none", "ductor-repl-main-mx-1-none"})

    assert pool.kill(transport="tg", chat=1, topic=None) == 1

    assert "ductor-repl-main-tg-1-none" not in runner.sessions
    assert "ductor-repl-main-mx-1-none" in runner.sessions


def test_same_transport_chat_topic_reuses_session(tmp_path: Path) -> None:
    runner = FakeRunner()
    claude_home = tmp_path / "claude"
    signal_dir = tmp_path / "signals"
    _write_transcript(claude_home, "sess-1", "aaa111", "answer")
    _touch_signal(signal_dir, "main", "sess-1", "aaa111")
    pool = ReplPool(
        _config(tmp_path, claude_home=claude_home, signal_dir=signal_dir),
        runner=runner,
        token_factory=lambda: "aaa111",
    )

    pool.send(transport="tg", chat=1, topic=None, model="sonnet", system_prompt="s", prompt="hi")
    spawns = runner.count("tmux", "new-session")
    _touch_signal(signal_dir, "main", "sess-1", "aaa111")
    pool.send(transport="tg", chat=1, topic=None, model="sonnet", system_prompt="s", prompt="hi")

    assert runner.count("tmux", "new-session") == spawns  # reused, no respawn
