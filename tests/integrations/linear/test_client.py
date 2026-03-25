"""HTTP client tests for Linear integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Self

import pytest

import ductor_bot.integrations.linear.client as linear_client_module
from ductor_bot.integrations.linear.client import LinearClient
from ductor_bot.integrations.linear.config import LinearConfig


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
    closed: bool = False
    calls: list[dict[str, object]] = field(default_factory=list)
    init_kwargs: dict[str, object] = field(default_factory=dict)

    def post(self, url: str, *, json: dict[str, object]) -> _MockResponse:
        self.calls.append({"url": url, "json": json})
        if not self.responses:
            msg = "No mock response configured"
            raise AssertionError(msg)
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def install_mock_session(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[list[_MockResponse]], _MockSession]:
    def _install(responses: list[_MockResponse]) -> _MockSession:
        session = _MockSession(responses=responses)

        def _factory(*_args: object, **kwargs: object) -> _MockSession:
            session.init_kwargs = dict(kwargs)
            return session

        monkeypatch.setattr(linear_client_module.aiohttp, "ClientSession", _factory)
        return session

    return _install


async def test_list_teams(
    install_mock_session: Callable[[list[_MockResponse]], _MockSession],
) -> None:
    session = install_mock_session(
        [
            _MockResponse(
                status=200,
                payload={
                    "data": {
                        "teams": {
                            "nodes": [
                                {"id": "team_1", "key": "SSU", "name": "Ssunbles"},
                            ]
                        }
                    }
                },
            )
        ]
    )
    client = LinearClient(LinearConfig(api_token="lin_api_token"))

    teams = await client.list_teams()

    assert len(teams) == 1
    assert teams[0].key == "SSU"
    headers = session.init_kwargs["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "lin_api_token"
    await client.close()
    assert session.closed is True


async def test_get_issue_not_found(
    install_mock_session: Callable[[list[_MockResponse]], _MockSession],
) -> None:
    install_mock_session([_MockResponse(status=200, payload={"data": {"issue": None}})])
    client = LinearClient(LinearConfig(api_token="lin_api_token"))

    issue = await client.get_issue("SSU-999")

    assert issue is None
    await client.close()


async def test_append_issue_description(
    install_mock_session: Callable[[list[_MockResponse]], _MockSession],
) -> None:
    session = install_mock_session(
        [
            _MockResponse(
                status=200,
                payload={
                    "data": {
                        "issue": {
                            "id": "issue_1",
                            "identifier": "SSU-1",
                            "title": "Test",
                            "url": "https://linear.app/ssu/issue/SSU-1/test",
                            "description": "Initial description",
                            "state": {"name": "Todo"},
                        }
                    }
                },
            ),
            _MockResponse(
                status=200,
                payload={
                    "data": {
                        "issueUpdate": {
                            "issue": {
                                "id": "issue_1",
                                "identifier": "SSU-1",
                                "title": "Test",
                                "url": "https://linear.app/ssu/issue/SSU-1/test",
                                "description": "Initial description\n\nAppendix",
                                "state": {"name": "Todo"},
                            }
                        }
                    }
                },
            ),
        ]
    )
    client = LinearClient(LinearConfig(api_token="lin_api_token"))

    updated = await client.append_issue_description("SSU-1", "Appendix")

    assert "Appendix" in updated.description
    second_payload = session.calls[1]["json"]
    assert isinstance(second_payload, dict)
    variables = second_payload["variables"]
    assert isinstance(variables, dict)
    assert variables["description"] == "Initial description\n\nAppendix"
    await client.close()


async def test_set_issue_state_by_name(
    install_mock_session: Callable[[list[_MockResponse]], _MockSession],
) -> None:
    session = install_mock_session(
        [
            _MockResponse(
                status=200,
                payload={
                    "data": {
                        "issue": {
                            "id": "issue_2",
                            "state": {"name": "Todo"},
                            "team": {
                                "states": {
                                    "nodes": [
                                        {"id": "state_1", "name": "Todo"},
                                        {"id": "state_2", "name": "In Progress"},
                                    ]
                                }
                            },
                        }
                    }
                },
            ),
            _MockResponse(
                status=200,
                payload={
                    "data": {
                        "issueUpdate": {
                            "issue": {
                                "state": {
                                    "name": "In Progress",
                                }
                            }
                        }
                    }
                },
            ),
        ]
    )
    client = LinearClient(LinearConfig(api_token="lin_api_token"))

    state_name = await client.set_issue_state_by_name("SSU-2", ["in progress", "done"])

    assert state_name == "In Progress"
    second_payload = session.calls[1]["json"]
    assert isinstance(second_payload, dict)
    variables = second_payload["variables"]
    assert isinstance(variables, dict)
    assert variables["stateId"] == "state_2"
    await client.close()


async def test_graphql_error_raises_runtime_error(
    install_mock_session: Callable[[list[_MockResponse]], _MockSession],
) -> None:
    install_mock_session(
        [
            _MockResponse(
                status=200,
                payload={"errors": [{"message": "broken"}]},
            )
        ]
    )
    client = LinearClient(LinearConfig(api_token="lin_api_token"))

    with pytest.raises(RuntimeError, match="broken"):
        await client.list_teams()

    await client.close()
