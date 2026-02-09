"""Tests for PyPI version checking."""

from __future__ import annotations

import importlib.metadata
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.infra.version import (
    VersionInfo,
    _parse_version,
    check_pypi,
    fetch_changelog,
    get_current_version,
)


class TestParseVersion:
    """Test dotted version string parsing."""

    def test_standard_triple(self) -> None:
        assert _parse_version("1.2.3") == (1, 2, 3)

    def test_single_digit(self) -> None:
        assert _parse_version("5") == (5,)

    def test_four_segments(self) -> None:
        assert _parse_version("1.0.0.1") == (1, 0, 0, 1)

    def test_non_numeric_suffix_stops(self) -> None:
        assert _parse_version("1.2.3a1") == (1, 2)

    def test_empty_string(self) -> None:
        assert _parse_version("") == ()

    def test_comparison_newer(self) -> None:
        assert _parse_version("2.0.0") > _parse_version("1.9.9")

    def test_comparison_equal(self) -> None:
        assert _parse_version("1.0.0") == _parse_version("1.0.0")

    def test_comparison_older(self) -> None:
        assert _parse_version("0.1.0") < _parse_version("0.2.0")

    def test_comparison_minor_bump(self) -> None:
        assert _parse_version("1.1.0") > _parse_version("1.0.99")


class TestGetCurrentVersion:
    """Test installed version detection."""

    def test_returns_installed_version(self) -> None:
        with patch("ductor_bot.infra.version.importlib.metadata.version", return_value="1.5.0"):
            assert get_current_version() == "1.5.0"

    def test_returns_fallback_when_not_installed(self) -> None:
        with patch(
            "ductor_bot.infra.version.importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError,
        ):
            assert get_current_version() == "0.0.0"


def _mock_pypi_session(
    *, status: int = 200, json_data: dict | None = None, error: Exception | None = None
) -> MagicMock:
    """Build a mock aiohttp.ClientSession for check_pypi tests.

    Handles the combined ``async with (ClientSession() as s, s.get() as r)`` pattern.
    """
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})

    @asynccontextmanager
    async def mock_get(*_args: object, **_kwargs: object) -> AsyncGenerator[MagicMock, None]:
        if error:
            raise error
        yield resp

    session = MagicMock()
    session.get = mock_get

    @asynccontextmanager
    async def mock_session_cm(**_kwargs: object) -> AsyncGenerator[MagicMock, None]:
        yield session

    return mock_session_cm


class TestCheckPypi:
    """Test PyPI API response handling."""

    async def test_returns_version_info_when_update_available(self) -> None:
        mock = _mock_pypi_session(
            json_data={"info": {"version": "2.0.0", "summary": "A great update"}}
        )

        with (
            patch("ductor_bot.infra.version.get_current_version", return_value="1.0.0"),
            patch("ductor_bot.infra.version.aiohttp.ClientSession", mock),
        ):
            result = await check_pypi()

        assert result is not None
        assert result.current == "1.0.0"
        assert result.latest == "2.0.0"
        assert result.update_available is True
        assert result.summary == "A great update"

    async def test_no_update_when_same_version(self) -> None:
        mock = _mock_pypi_session(json_data={"info": {"version": "1.0.0", "summary": "Current"}})

        with (
            patch("ductor_bot.infra.version.get_current_version", return_value="1.0.0"),
            patch("ductor_bot.infra.version.aiohttp.ClientSession", mock),
        ):
            result = await check_pypi()

        assert result is not None
        assert result.update_available is False

    async def test_returns_none_on_http_error(self) -> None:
        mock = _mock_pypi_session(status=500)

        with patch("ductor_bot.infra.version.aiohttp.ClientSession", mock):
            result = await check_pypi()

        assert result is None

    async def test_returns_none_on_network_error(self) -> None:
        import aiohttp

        mock = _mock_pypi_session(error=aiohttp.ClientError())

        with patch("ductor_bot.infra.version.aiohttp.ClientSession", mock):
            result = await check_pypi()

        assert result is None

    async def test_returns_none_on_missing_version_field(self) -> None:
        mock = _mock_pypi_session(json_data={"info": {}})

        with patch("ductor_bot.infra.version.aiohttp.ClientSession", mock):
            result = await check_pypi()

        assert result is None

    async def test_returns_none_on_empty_info(self) -> None:
        mock = _mock_pypi_session(json_data={})

        with patch("ductor_bot.infra.version.aiohttp.ClientSession", mock):
            result = await check_pypi()

        assert result is None

    def test_version_info_is_frozen(self) -> None:
        info = VersionInfo(current="1.0.0", latest="2.0.0", update_available=True, summary="test")
        assert info.current == "1.0.0"
        assert info.update_available is True


class TestFetchChangelog:
    """Test GitHub Releases changelog fetching."""

    async def test_returns_body_for_v_prefixed_tag(self) -> None:
        mock = _mock_pypi_session(json_data={"body": "## What's new\n\n- Feature A"})
        with patch("ductor_bot.infra.version.aiohttp.ClientSession", mock):
            result = await fetch_changelog("1.0.0")
        assert result is not None
        assert "Feature A" in result

    async def test_returns_none_on_404(self) -> None:
        mock = _mock_pypi_session(status=404)
        with patch("ductor_bot.infra.version.aiohttp.ClientSession", mock):
            result = await fetch_changelog("99.0.0")
        assert result is None

    async def test_returns_none_on_network_error(self) -> None:
        import aiohttp

        mock = _mock_pypi_session(error=aiohttp.ClientError())
        with patch("ductor_bot.infra.version.aiohttp.ClientSession", mock):
            result = await fetch_changelog("1.0.0")
        assert result is None

    async def test_returns_none_on_empty_body(self) -> None:
        mock = _mock_pypi_session(json_data={"body": ""})
        with patch("ductor_bot.infra.version.aiohttp.ClientSession", mock):
            result = await fetch_changelog("1.0.0")
        assert result is None

    async def test_strips_whitespace(self) -> None:
        mock = _mock_pypi_session(json_data={"body": "  changelog text  \n\n"})
        with patch("ductor_bot.infra.version.aiohttp.ClientSession", mock):
            result = await fetch_changelog("1.0.0")
        assert result == "changelog text"
