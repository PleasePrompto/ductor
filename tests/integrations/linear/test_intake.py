"""Tests for AI intake structuring flow."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Self

import pytest

import ductor_bot.integrations.linear.intake as intake_module
from ductor_bot.integrations.linear.intake import structure_task


@dataclass
class _MockResponse:
    status: int
    payload: object

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, *, content_type: str | None = None) -> object:
        del content_type
        return self.payload


@dataclass
class _MockSession:
    responses: list[_MockResponse]
    init_kwargs: dict[str, object] = field(default_factory=dict)
    calls: list[dict[str, object]] = field(default_factory=list)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def post(self, url: str, **kwargs: object) -> _MockResponse:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            msg = "No mock responses configured"
            raise AssertionError(msg)
        return self.responses.pop(0)


@pytest.fixture
def install_mock_session(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[list[_MockResponse]], _MockSession]:
    def _install(responses: list[_MockResponse]) -> _MockSession:
        session = _MockSession(responses=responses)

        def _factory(*_args: object, **kwargs: object) -> _MockSession:
            session.init_kwargs = dict(kwargs)
            return session

        monkeypatch.setattr(intake_module.aiohttp, "ClientSession", _factory)
        return session

    return _install


async def test_structure_task_passthrough() -> None:
    draft = await structure_task("сырой текст", provider="passthrough")

    assert draft.title == "сырой текст"
    assert draft.description == "сырой текст"
    assert draft.acceptance == ""


async def test_structure_task_openai_parses_json(
    install_mock_session: Callable[[list[_MockResponse]], _MockSession],
) -> None:
    session = install_mock_session(
        [
            _MockResponse(
                status=200,
                payload={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"title":"Тестовая задача",'
                                    '"description":"## Контекст\\nctx",'
                                    '"acceptance":"- ok",'
                                    '"priority":2}'
                                )
                            }
                        }
                    ]
                },
            )
        ]
    )

    draft = await structure_task(
        "нужно сделать выгрузку",
        provider="openai",
        model="gpt-4.1-mini",
        api_key="openai-key",
    )

    assert draft.title == "Тестовая задача"
    assert draft.acceptance == "- ok"
    assert draft.priority == 2

    call = session.calls[0]
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    headers = call["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer openai-key"


async def test_structure_task_anthropic_extracts_embedded_json(
    install_mock_session: Callable[[list[_MockResponse]], _MockSession],
) -> None:
    install_mock_session(
        [
            _MockResponse(
                status=200,
                payload={
                    "content": [
                        {
                            "text": (
                                "Вот результат:\n"
                                '{"title":"Задача из Claude",'
                                '"description":"## Контекст\\nctx",'
                                '"acceptance":"- done",'
                                '"priority":3}\n'
                                "Спасибо"
                            )
                        }
                    ]
                },
            )
        ]
    )

    draft = await structure_task(
        "разобрать бриф",
        provider="anthropic",
        model="claude-3-5-sonnet",
        api_key="anthropic-key",
    )

    assert draft.title == "Задача из Claude"
    assert draft.acceptance == "- done"
    assert draft.priority == 3


async def test_structure_task_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown intake provider"):
        await structure_task("text", provider="invalid")
