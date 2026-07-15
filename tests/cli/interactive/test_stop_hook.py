from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import ductor_bot.cli.interactive.stop_hook as stop_hook_module
from ductor_bot.cli.interactive.stop_hook import (
    extract_last_user_nonce,
    handle_stop_payload,
    merge_stop_hook_settings,
)


def _write_jsonl(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def test_extract_last_user_nonce_from_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"content": "old [[reply-token:1111]]"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
            {
                "type": "user",
                "message": {"content": [{"type": "text", "text": "new [[reply-token:abcd1234]]"}]},
            },
        ],
    )

    assert extract_last_user_nonce(transcript) == "abcd1234"


def test_extract_last_user_nonce_skips_tool_result_user_events(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"content": "prompt [[reply-token:facefeed]]"}},
            {"type": "assistant", "message": {"content": "using memory"}},
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "Read 1 file"}]},
            },
        ],
    )

    assert extract_last_user_nonce(transcript) == "facefeed"


def test_extract_last_user_nonce_ignores_tool_result_token_echo(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"content": "prompt [[reply-token:aaaa1111]]"}},
            {"type": "assistant", "message": {"content": "running tool"}},
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "content": "echo [[reply-token:bbbb2222]]"}]
                },
            },
        ],
    )

    assert extract_last_user_nonce(transcript) == "aaaa1111"


def test_extract_last_user_nonce_returns_latest_prompt_nonce_across_tool_results(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"content": "first [[reply-token:1111aaaa]]"}},
            {"type": "assistant", "message": {"content": "using tool"}},
            {"type": "user", "message": {"content": "tool result without nonce"}},
            {"type": "user", "message": {"content": "second [[reply-token:2222bbbb]]"}},
            {"type": "assistant", "message": {"content": "using another tool"}},
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "wrote memories"}]},
            },
        ],
    )

    assert extract_last_user_nonce(transcript) == "2222bbbb"


def test_handle_stop_payload_touches_0600_signal(tmp_path: Path) -> None:
    transcript = tmp_path / "projects" / "hash" / "sess-1.jsonl"
    _write_jsonl(
        transcript,
        [{"type": "user", "message": {"content": "prompt [[reply-token:feedbeef]]"}}],
    )

    signal = handle_stop_payload(
        {"session_id": "sess-1", "transcript_path": str(transcript)},
        signal_dir=tmp_path / "signals",
        agent="main",
    )

    assert signal == tmp_path / "signals" / "main" / "sess-1.feedbeef.done"
    assert signal is not None
    assert signal.exists()
    assert stat.S_IMODE(signal.stat().st_mode) == 0o600


def test_merge_stop_hook_settings_preserves_existing_hooks(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [{"matcher": "old", "hooks": [{"type": "command", "command": "old"}]}]
                }
            }
        ),
        encoding="utf-8",
    )

    merge_stop_hook_settings(settings, command="python -m hook")
    payload = json.loads(settings.read_text(encoding="utf-8"))

    assert settings.with_suffix(".json.bak").exists()
    assert payload["hooks"]["Stop"][0]["matcher"] == "old"
    assert payload["hooks"]["Stop"][1] == {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "python -m hook"}],
    }


def test_stop_hook_script_runs_standalone_from_file_path(tmp_path: Path) -> None:
    transcript = tmp_path / "projects" / "hash" / "sess-standalone.jsonl"
    _write_jsonl(
        transcript,
        [{"type": "user", "message": {"content": "prompt [[reply-token:cafebabe]]"}}],
    )
    script_path = Path(stop_hook_module.__file__)

    result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--signal-dir",
            str(tmp_path / "signals"),
            "--agent",
            "main",
        ],
        input=json.dumps({"session_id": "sess-standalone", "transcript_path": str(transcript)}),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    signal = tmp_path / "signals" / "main" / "sess-standalone.cafebabe.done"
    assert signal.exists()
    assert stat.S_IMODE(signal.stat().st_mode) == 0o600
