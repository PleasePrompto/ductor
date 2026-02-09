"""Package version checking against PyPI and GitHub Releases."""

from __future__ import annotations

import importlib.metadata
import logging
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

_PYPI_URL = "https://pypi.org/pypi/ductor/json"
_GITHUB_RELEASES_URL = "https://api.github.com/repos/PleasePrompto/ductor/releases"
_PACKAGE_NAME = "ductor"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


def get_current_version() -> str:
    """Return the installed version of ductor."""
    try:
        return importlib.metadata.version(_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse dotted version string into a comparable tuple."""
    parts: list[int] = []
    for segment in v.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            break
    return tuple(parts)


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """Result of a PyPI version check."""

    current: str
    latest: str
    update_available: bool
    summary: str


async def check_pypi() -> VersionInfo | None:
    """Check PyPI for the latest version. Returns None on failure."""
    current = get_current_version()
    try:
        async with (
            aiohttp.ClientSession(timeout=_TIMEOUT) as session,
            session.get(_PYPI_URL) as resp,
        ):
            if resp.status != 200:
                return None
            data = await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError):
        logger.debug("PyPI version check failed", exc_info=True)
        return None

    info = data.get("info", {})
    latest = info.get("version", "")
    if not latest:
        return None

    summary = info.get("summary", "")
    update_available = _parse_version(latest) > _parse_version(current)
    return VersionInfo(
        current=current,
        latest=latest,
        update_available=update_available,
        summary=summary,
    )


async def fetch_changelog(version: str) -> str | None:
    """Fetch release notes for *version* from GitHub Releases.

    Tries ``v{version}`` tag first, then ``{version}`` without prefix.
    Returns the release body (Markdown) or ``None`` on failure.
    """
    headers = {"Accept": "application/vnd.github+json"}
    for tag in (f"v{version}", version):
        url = f"{_GITHUB_RELEASES_URL}/tags/{tag}"
        try:
            async with (
                aiohttp.ClientSession(timeout=_TIMEOUT, headers=headers) as session,
                session.get(url) as resp,
            ):
                if resp.status != 200:
                    continue
                data = await resp.json()
                body: str = data.get("body", "")
                if body:
                    return body.strip()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            logger.debug("GitHub release fetch failed for tag %s", tag, exc_info=True)
    return None
