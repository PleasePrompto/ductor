"""Short, stable tokens for addressing filesystem paths in callback data.

Telegram caps ``callback_data`` at 64 bytes. Encoding a relative path directly
works only until the tree gets deep — ``sf:Phoenix/Phoenix-Telegram-MiniApp/
src/components/charts`` already exceeds it, and the button then fails at send
time rather than anywhere obvious.

Paths are therefore addressed by a hash prefix and resolved through a registry
populated as views are rendered. The token is derived from the path, so
re-rendering a directory produces the same token and a button from an earlier
message keeps working for as long as the entry is retained.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from pathlib import Path

#: 10 hex chars keeps ``sf:<token>`` at 13 bytes, far inside the limit, while
#: collisions remain implausible for the number of paths a browser ever shows.
_TOKEN_CHARS = 10

#: Bounded so a long-running bot cannot grow this without limit. Oldest entries
#: are evicted first; a button whose entry has been evicted falls back to the
#: root view rather than erroring.
_MAX_ENTRIES = 4000

_registry: OrderedDict[str, Path] = OrderedDict()


def token_for(path: Path) -> str:
    """Return the token for *path*, registering it for later resolution."""
    resolved = path.resolve()
    token = hashlib.sha256(str(resolved).encode()).hexdigest()[:_TOKEN_CHARS]
    _registry[token] = resolved
    _registry.move_to_end(token)
    while len(_registry) > _MAX_ENTRIES:
        _registry.popitem(last=False)
    return token


def path_for(token: str) -> Path | None:
    """Return the path for *token*, or ``None`` if it is unknown or evicted."""
    path = _registry.get(token)
    if path is not None:
        _registry.move_to_end(token)
    return path


def clear() -> None:
    """Drop every registered path. For tests."""
    _registry.clear()


def size() -> int:
    """Number of currently registered paths. For tests."""
    return len(_registry)
