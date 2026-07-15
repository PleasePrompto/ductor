"""Tests for command-log environment redaction."""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from ductor_bot.cli._log_redact import redact_cmd_for_log
from ductor_bot.cli.antigravity_provider import _safe_command_for_logging
from ductor_bot.cli.claude_provider import _log_cmd as log_claude_cmd
from ductor_bot.cli.codex_provider import _log_cmd as log_codex_cmd
from ductor_bot.cli.gemini_provider import _log_cmd as log_gemini_cmd

_FAKE_SECRET = "ghp_FAKESECRET123"
_CMD = [
    "docker",
    "exec",
    "-e",
    f"GITHUB_TOKEN={_FAKE_SECRET}",
    "-e",
    "DUCTOR_CHAT_ID=42",
    "--model",
    "opus",
]


def test_redact_cmd_for_log_masks_secret_and_preserves_structure() -> None:
    redacted = redact_cmd_for_log(_CMD)
    rendered = " ".join(redacted)

    assert _FAKE_SECRET not in rendered
    assert "GITHUB_TOKEN=***" in rendered
    assert "DUCTOR_CHAT_ID=42" in rendered
    assert redacted[-2:] == ["--model", "opus"]


def test_redact_cmd_for_log_handles_inline_env_forms() -> None:
    redacted = redact_cmd_for_log(
        [
            "docker",
            "--env=API_TOKEN=inline-secret",
            "-eINLINE_TOKEN=compact-secret",
            "SERVICE_URL=https://example.test",
        ]
    )

    assert redacted == [
        "docker",
        "--env=API_TOKEN=***",
        "-eINLINE_TOKEN=***",
        "SERVICE_URL=https://example.test",
    ]


@pytest.mark.parametrize(
    "log_cmd",
    [log_claude_cmd, log_codex_cmd, log_gemini_cmd],
    ids=["claude", "codex", "gemini"],
)
def test_provider_command_logs_redact_secret(
    caplog: pytest.LogCaptureFixture,
    log_cmd: Callable[[list[str]], None],
) -> None:
    with caplog.at_level(logging.INFO):
        log_cmd(_CMD)

    assert _FAKE_SECRET not in caplog.text
    assert "GITHUB_TOKEN=***" in caplog.text
    assert "DUCTOR_CHAT_ID=42" in caplog.text


def test_antigravity_safe_command_redacts_before_truncation() -> None:
    safe = _safe_command_for_logging(_CMD)
    rendered = " ".join(safe)

    assert _FAKE_SECRET not in rendered
    assert "GITHUB_TOKEN=***" in rendered
    assert "DUCTOR_CHAT_ID=42" in rendered
