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


def apply_to_env(env: dict[str, str], account_dir: str | None) -> dict[str, str]:
    """Set or clear ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` in *env*, in place.

    Clearing means removing the key entirely. Setting it to an empty string is
    not equivalent: Claude Code reads an empty value as ``~/.claude``, which
    would silently ignore a custom ``CLAUDE_CONFIG_DIR``.
    """
    if account_dir:
        env[ENV_VAR] = account_dir
    else:
        env.pop(ENV_VAR, None)
    return env


def usable_accounts(accounts: Mapping[str, str]) -> dict[str, str]:
    """Drop entries whose path is blank.

    A name mapped to an empty or whitespace path passes a naive membership check
    but resolves to the default store, so the UI would report an account that is
    not the one being used. Such entries are treated as not configured.
    """
    return {name: path for name, path in accounts.items() if path and path.strip()}


def account_names(accounts: Mapping[str, str]) -> list[str]:
    """Return usable account names in a stable, display-friendly order."""
    return sorted(usable_accounts(accounts))


def is_known_account(accounts: Mapping[str, str], name: str) -> bool:
    """Return ``True`` for the default account ("") or a usable configured name."""
    return not name or name in usable_accounts(accounts)


def active_claude_account_dir(config: object) -> str:
    """Resolved credential store for *config*'s selected account, or "".

    Shared by every auth probe so status, startup and the welcome screen all
    describe the account the agent will actually run as.
    """
    from ductor_bot.cli.claude_accounts import resolve_account_dir

    return (
        resolve_account_dir(
            getattr(config, "claude_accounts", {}) or {},
            getattr(config, "claude_account", "") or "",
        )
        or ""
    )
