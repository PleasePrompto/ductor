"""Inline directive parser: extract @model and future @key=value directives."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_DIRECTIVE_RE = re.compile(r"@([a-zA-Z][a-zA-Z0-9_-]*)(?:=(\S+))?")


@dataclass(frozen=True, slots=True)
class ParsedDirectives:
    """Result of parsing inline directives from a message."""

    cleaned: str
    model: str | None = None
    raw_directives: dict[str, str | None] = field(default_factory=dict)

    @property
    def has_model(self) -> bool:
        return self.model is not None

    @property
    def is_directive_only(self) -> bool:
        return not self.cleaned


def parse_directives(text: str, known_models: frozenset[str]) -> ParsedDirectives:
    """Parse leading @directives from message text.

    Only directives at the very start of the message are consumed.
    This prevents false matches like "email @opus".
    """
    stripped = text.strip()
    if not stripped or not stripped.startswith("@"):
        return ParsedDirectives(cleaned=stripped)

    model: str | None = None
    raw_directives: dict[str, str | None] = {}
    pos = 0

    for match in _DIRECTIVE_RE.finditer(stripped):
        prefix = stripped[pos : match.start()]
        if prefix.strip():
            break

        key = match.group(1).lower()
        value: str | None = match.group(2)

        if key in known_models and model is None:
            model = key
        else:
            raw_directives[key] = value

        pos = match.end()

    if model is None and not raw_directives:
        return ParsedDirectives(cleaned=stripped)

    cleaned = stripped[pos:].strip()
    logger.debug("Directive parsed model=%s cleaned=%s", model, bool(cleaned))
    return ParsedDirectives(cleaned=cleaned, model=model, raw_directives=raw_directives)
