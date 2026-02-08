"""Detect how ductor-bot was installed (pipx, pip, or dev/source)."""

from __future__ import annotations

import json
import logging
import sys
from importlib.metadata import distribution
from typing import Literal

logger = logging.getLogger(__name__)

InstallMode = Literal["pipx", "pip", "dev"]

_PACKAGE_NAME = "ductor-bot"


def detect_install_mode() -> InstallMode:
    """Detect installation method at runtime.

    Returns:
        ``"pipx"`` -- installed via ``pipx install ductor-bot``
        ``"pip"``  -- installed via ``pip install ductor-bot`` (from PyPI)
        ``"dev"``  -- editable install (``pip install -e .``) or running from source
    """
    if "pipx" in sys.prefix:
        return "pipx"

    try:
        dist = distribution(_PACKAGE_NAME)
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            url_info = json.loads(direct_url_text)
            if url_info.get("dir_info", {}).get("editable", False):
                return "dev"
    except Exception:
        return "dev"

    return "pip"


def is_upgradeable() -> bool:
    """Return True if the bot can self-upgrade (pipx or pip, not dev)."""
    return detect_install_mode() != "dev"
