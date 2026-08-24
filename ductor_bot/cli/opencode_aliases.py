"""Short aliases for OpenCode provider IDs used in model directives."""

from __future__ import annotations

# Keep these short and explicit: OpenCode provider IDs are user/configuration
# dependent, while these aliases are intended as stable chat-facing shortcuts.
OPENCODE_PROVIDER_ALIASES: dict[str, str] = {
    "go": "opencode-go",
    "zen": "opencode",
    "or": "openrouter",
    "openrouter": "openrouter",
    "opencode": "opencode",
    "opencode-go": "opencode-go",
    # Normalize the provider ID used by earlier ductor builds.
    "opencode-zen": "opencode",
}

OPENCODE_PROVIDER_SHORT_NAMES: dict[str, str] = {
    "opencode": "zen",
    "opencode-go": "go",
    "openrouter": "or",
}


def expand_opencode_model_alias(value: str) -> str:
    """Expand ``go/model``-style shorthand to ``provider/model``.

    Values without a recognized OpenCode provider alias are returned unchanged.
    """
    provider, separator, model = value.partition("/")
    if not separator or not model:
        return value
    canonical_provider = OPENCODE_PROVIDER_ALIASES.get(provider.lower())
    if canonical_provider is None:
        return value
    return f"{canonical_provider}/{model}"


def shorten_opencode_model_id(value: str) -> str:
    """Return a compact directive-style label for an OpenCode model ID."""
    provider, separator, model = value.partition("/")
    if not separator or not model:
        return value
    short_provider = OPENCODE_PROVIDER_SHORT_NAMES.get(provider, provider)
    return f"@{short_provider}/{model}"
