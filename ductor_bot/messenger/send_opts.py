"""Shared send-options base for all messenger transports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class BaseSendOpts:
    """Shared send options across all transports."""

    allowed_roots: Sequence[Path] | None = None
    parse_file_tags: bool = True
