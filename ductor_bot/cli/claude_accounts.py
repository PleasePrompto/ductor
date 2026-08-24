"""Claude credential-store account switching.

Claude Code reads its OAuth credentials from the directory named by
``CLAUDE_SECURESTORAGE_CONFIG_DIR`` (falling back to the regular config dir).
Only the *credential store* moves — ``CLAUDE_CONFIG_DIR`` is left alone, so
sessions, projects, skills, MCP servers and settings stay shared.

That split is what makes account switching useful mid-conversation: when one
subscription hits its rate limit, pointing the credential store at a second
account lets ``claude --resume`` continue the *same* session on the other
subscription, exactly like running a wrapper script that exports the variable.

Platform note: on macOS the directory is hashed into the Keychain service name;
on Linux it holds a ``.credentials.json`` file. Both honour the variable, so the
same config works on either host.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

#: Environment variable Claude Code reads the credential-store path from.
ENV_VAR = "CLAUDE_SECURESTORAGE_CONFIG_DIR"


def resolve_account_dir(accounts: Mapping[str, str], active: str) -> str | None:
    """Return the credential-store directory for the *active* account.

    Returns ``None`` when the default store should be used — either because no
    account is selected, the name is unknown, or its configured path is empty.
    ``None`` means "leave ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` unset", which is
    not the same as setting it to an empty string (Claude Code treats an empty
    value as ``~/.claude``, ignoring a custom ``CLAUDE_CONFIG_DIR``).
    """
    if not active:
        return None
    raw = accounts.get(active, "").strip()
    if not raw:
        return None
    return str(Path(raw).expanduser())


def account_names(accounts: Mapping[str, str]) -> list[str]:
    """Return configured account names in a stable, display-friendly order."""
    return sorted(accounts)


def is_known_account(accounts: Mapping[str, str], name: str) -> bool:
    """Return ``True`` for the default account ("") or a configured name."""
    return not name or name in accounts
