"""Tests for dynamic Codex model discovery."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.codex_discovery import CodexModelInfo, discover_codex_models

_INIT_RESPONSE = json.dumps(
    {
        "id": 1,
        "result": {"userAgent": "ductor/0.98.0"},
    }
)

_MODEL_LIST_RESPONSE = json.dumps(
    {
        "id": 2,
        "result": {
            "data": [
                {
                    "id": "gpt-5.2-codex",
                    "displayName": "gpt-5.2-codex",
                    "description": "Frontier agentic coding model.",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low", "description": "Fast"},
                        {"reasoningEffort": "medium", "description": "Balanced"},
                        {"reasoningEffort": "high", "description": "Deep"},
                        {"reasoningEffort": "xhigh", "description": "Extra deep"},
                    ],
                    "defaultReasoningEffort": "medium",
                    "isDefault": True,
                },
                {
                    "id": "gpt-5.1-codex-mini",
                    "displayName": "gpt-5.1-codex-mini",
                    "description": "Cheaper, faster.",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "medium", "description": "Default"},
                        {"reasoningEffort": "high", "description": "Deep"},
                    ],
                    "defaultReasoningEffort": "medium",
                    "isDefault": False,
                },
            ],
            "nextCursor": None,
        },
    }
)

_STDOUT = f"{_INIT_RESPONSE}\n{_MODEL_LIST_RESPONSE}\n"


def _mock_process(stdout: str = _STDOUT, returncode: int = 0) -> AsyncMock:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout.encode(), b""))
    proc.returncode = returncode
    proc.kill = AsyncMock()
    return proc


async def test_discover_models_parses_response() -> None:
    proc = _mock_process()
    with (
        patch("ductor_bot.cli.codex_discovery.which", return_value="/usr/bin/codex"),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        models = await discover_codex_models()

    assert len(models) == 2

    first = models[0]
    assert first.id == "gpt-5.2-codex"
    assert first.display_name == "gpt-5.2-codex"
    assert first.supported_efforts == ("low", "medium", "high", "xhigh")
    assert first.default_effort == "medium"
    assert first.is_default is True

    second = models[1]
    assert second.id == "gpt-5.1-codex-mini"
    assert second.supported_efforts == ("medium", "high")
    assert second.is_default is False


async def test_discover_models_codex_not_installed() -> None:
    with patch("ductor_bot.cli.codex_discovery.which", return_value=None):
        models = await discover_codex_models()
    assert models == []


async def test_discover_models_timeout() -> None:
    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=TimeoutError)
    proc.kill = MagicMock()

    with (
        patch("ductor_bot.cli.codex_discovery.which", return_value="/usr/bin/codex"),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        models = await discover_codex_models(deadline=0.1)

    assert models == []


async def test_discover_models_invalid_json() -> None:
    proc = _mock_process(stdout="not json at all\n")
    with (
        patch("ductor_bot.cli.codex_discovery.which", return_value="/usr/bin/codex"),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        models = await discover_codex_models()
    assert models == []


async def test_discover_models_spawn_error() -> None:
    with (
        patch("ductor_bot.cli.codex_discovery.which", return_value="/usr/bin/codex"),
        patch("asyncio.create_subprocess_exec", side_effect=OSError("exec failed")),
    ):
        models = await discover_codex_models()
    assert models == []


async def test_codex_model_info_is_frozen() -> None:
    info = CodexModelInfo(
        id="test",
        display_name="Test",
        description="A test model",
        supported_efforts=("medium", "high"),
        default_effort="medium",
        is_default=False,
    )
    with pytest.raises(AttributeError):
        info.id = "changed"  # type: ignore[misc]
