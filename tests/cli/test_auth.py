"""Tests for CLI auth detection."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.auth import (
    AuthResult,
    AuthStatus,
    check_antigravity_auth,
    check_claude_auth,
    check_codex_auth,
    check_gemini_auth,
    format_age,
    gemini_uses_api_key_mode,
)

if TYPE_CHECKING:
    import pytest


def test_auth_status_values() -> None:
    assert AuthStatus.AUTHENTICATED.value == "authenticated"
    assert AuthStatus.INSTALLED.value == "installed"
    assert AuthStatus.NOT_FOUND.value == "not_found"


def test_auth_result_is_authenticated() -> None:
    result = AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED)
    assert result.is_authenticated is True


def test_auth_result_not_authenticated() -> None:
    result = AuthResult(provider="claude", status=AuthStatus.INSTALLED)
    assert result.is_authenticated is False


def test_auth_result_age_human_none() -> None:
    result = AuthResult(provider="claude", status=AuthStatus.NOT_FOUND)
    assert result.age_human == ""


def test_format_age_seconds() -> None:
    from datetime import UTC, datetime, timedelta

    dt = datetime.now(UTC) - timedelta(seconds=30)
    assert format_age(dt) == "30s ago"


def test_format_age_minutes() -> None:
    from datetime import UTC, datetime, timedelta

    dt = datetime.now(UTC) - timedelta(minutes=5)
    assert format_age(dt) == "5m ago"


def test_format_age_hours() -> None:
    from datetime import UTC, datetime, timedelta

    dt = datetime.now(UTC) - timedelta(hours=3)
    assert format_age(dt) == "3h ago"


def test_format_age_days() -> None:
    from datetime import UTC, datetime, timedelta

    dt = datetime.now(UTC) - timedelta(days=2)
    assert format_age(dt) == "2d ago"


def _patch_claude_cli_fallback(monkeypatch: pytest.MonkeyPatch, *, logged_in: bool = False) -> None:
    """Disable the subprocess fallback so tests stay fast and deterministic."""
    import ductor_bot.cli.auth as _auth_mod

    # Accepts the optional account_dir the account-aware probe now passes.
    monkeypatch.setattr(_auth_mod, "_claude_cli_logged_in", lambda *_args: logged_in)


def test_check_claude_auth_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_claude_cli_fallback(monkeypatch)
    result = check_claude_auth()
    assert result.status == AuthStatus.NOT_FOUND


def test_check_claude_auth_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_claude_cli_fallback(monkeypatch)
    (tmp_path / ".claude").mkdir()
    result = check_claude_auth()
    assert result.status == AuthStatus.INSTALLED


def test_check_claude_auth_authenticated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_claude_cli_fallback(monkeypatch)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text("{}")
    result = check_claude_auth()
    assert result.status == AuthStatus.AUTHENTICATED
    assert result.auth_file is not None


def test_check_claude_auth_env_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    _patch_claude_cli_fallback(monkeypatch)
    result = check_claude_auth()
    assert result.status == AuthStatus.AUTHENTICATED


def test_check_claude_auth_env_key_empty_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    _patch_claude_cli_fallback(monkeypatch)
    result = check_claude_auth()
    assert result.status == AuthStatus.NOT_FOUND


def test_check_claude_auth_cli_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_claude_cli_fallback(monkeypatch, logged_in=True)
    result = check_claude_auth()
    assert result.status == AuthStatus.AUTHENTICATED


def test_check_claude_auth_cli_fallback_not_logged_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_claude_cli_fallback(monkeypatch, logged_in=False)
    (tmp_path / ".claude").mkdir()
    result = check_claude_auth()
    assert result.status == AuthStatus.INSTALLED


def test_claude_cli_logged_in_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    import ductor_bot.cli.auth as _auth_mod

    class _FakeResult:
        stdout = '{"loggedIn": true, "authMethod": "claude.ai"}'

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _FakeResult())
    assert _auth_mod._claude_cli_logged_in() is True


def test_claude_cli_logged_in_returns_false_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    import ductor_bot.cli.auth as _auth_mod

    def _raise(*_a: object, **_kw: object) -> None:
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert _auth_mod._claude_cli_logged_in() is False


def test_claude_cli_logged_in_returns_false_when_not_logged_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    import ductor_bot.cli.auth as _auth_mod

    class _FakeResult:
        stdout = '{"loggedIn": false}'

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _FakeResult())
    assert _auth_mod._claude_cli_logged_in() is False


def test_claude_cli_logged_in_resolves_npm_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    # On Windows "claude" resolves to claude.cmd; the probe must use the
    # resolved path, not the bare name (#149).
    import subprocess

    import ductor_bot.cli.auth as _auth_mod

    class _FakeResult:
        stdout = '{"loggedIn": true}'

    captured: list[str] = []

    def _capture(cmd: list[str], *_a: object, **_kw: object) -> _FakeResult:
        captured.extend(cmd)
        return _FakeResult()

    monkeypatch.setattr(_auth_mod.shutil, "which", lambda name: f"/bin/{name}.cmd")
    monkeypatch.setattr(subprocess, "run", _capture)

    assert _auth_mod._claude_cli_logged_in() is True
    assert captured[0] == "/bin/claude.cmd"


def test_antigravity_cli_logged_in_resolves_npm_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    import ductor_bot.cli.auth as _auth_mod

    class _FakeResult:
        stdout = "Gemini 3.5 Flash"
        stderr = ""
        returncode = 0

    captured: list[str] = []

    def _capture(cmd: list[str], *_a: object, **_kw: object) -> _FakeResult:
        captured.extend(cmd)
        return _FakeResult()

    monkeypatch.setattr(_auth_mod.shutil, "which", lambda name: f"/bin/{name}.cmd")
    monkeypatch.setattr(subprocess, "run", _capture)

    assert _auth_mod._antigravity_cli_logged_in() is True
    assert captured[0] == "/bin/agy.cmd"


def test_check_codex_auth_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = check_codex_auth()
    assert result.status == AuthStatus.NOT_FOUND


def test_check_codex_auth_authenticated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = check_codex_auth()
    assert result.status == AuthStatus.AUTHENTICATED


def test_check_codex_auth_env_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    result = check_codex_auth()
    assert result.status == AuthStatus.AUTHENTICATED


def test_check_codex_auth_env_key_empty_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    result = check_codex_auth()
    assert result.status == AuthStatus.NOT_FOUND


def test_check_codex_auth_config_toml_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text("[mcp]")
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = check_codex_auth()
    assert result.status == AuthStatus.INSTALLED


def test_check_codex_auth_handles_home_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path.home() can raise RuntimeError on Windows when %USERPROFILE%/%HOMEDRIVE%/
    %HOMEPATH% are all unset. check_codex_auth must return NOT_FOUND instead of
    propagating the exception (otherwise the onboarding wizard aborts).
    """

    def _raise_runtime_error() -> Path:
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(Path, "home", _raise_runtime_error)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = check_codex_auth()

    assert result.provider == "codex"
    assert result.status == AuthStatus.NOT_FOUND


# -- Gemini auth --


def test_check_gemini_auth_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.setattr(
        _auth_mod,
        "find_gemini_cli",
        lambda: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    result = check_gemini_auth()
    assert result.status == AuthStatus.NOT_FOUND


def test_check_gemini_auth_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod, "find_gemini_cli", lambda: "/usr/bin/gemini")
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    result = check_gemini_auth()
    assert result.status == AuthStatus.INSTALLED


def test_check_gemini_auth_authenticated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod, "find_gemini_cli", lambda: "/usr/bin/gemini")
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    result = check_gemini_auth()
    assert result.status == AuthStatus.AUTHENTICATED


def test_check_gemini_auth_google_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod, "find_gemini_cli", lambda: "/usr/bin/gemini")
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    result = check_gemini_auth()
    assert result.status == AuthStatus.AUTHENTICATED


def test_check_gemini_auth_oauth_creds_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod, "find_gemini_cli", lambda: "/usr/bin/gemini")
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    gemini_home = tmp_path / ".gemini"
    gemini_home.mkdir(parents=True)
    oauth = gemini_home / "oauth_creds.json"
    oauth.write_text('{"access_token":"x"}')

    result = check_gemini_auth()

    assert result.status == AuthStatus.AUTHENTICATED
    assert result.auth_file == oauth


def test_check_gemini_auth_dotenv_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod, "find_gemini_cli", lambda: "/usr/bin/gemini")
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    gemini_home = tmp_path / ".gemini"
    gemini_home.mkdir(parents=True)
    dotenv = gemini_home / ".env"
    dotenv.write_text("GEMINI_API_KEY=test-from-dotenv\n")

    result = check_gemini_auth()

    assert result.status == AuthStatus.AUTHENTICATED
    assert result.auth_file == dotenv


def test_check_gemini_auth_uses_gemini_cli_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "ignored-home")
    monkeypatch.setattr(_auth_mod, "find_gemini_cli", lambda: "/usr/bin/gemini")
    custom_home = tmp_path / "custom-home"
    monkeypatch.setenv("GEMINI_CLI_HOME", str(custom_home))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    gemini_home = custom_home / ".gemini"
    gemini_home.mkdir(parents=True)
    oauth = gemini_home / "oauth_creds.json"
    oauth.write_text('{"access_token":"x"}')

    result = check_gemini_auth()

    assert result.status == AuthStatus.AUTHENTICATED
    assert result.auth_file == oauth


def test_check_gemini_auth_oauth_selected_type_with_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod, "find_gemini_cli", lambda: "/usr/bin/gemini")
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    gemini_home = tmp_path / ".gemini"
    gemini_home.mkdir(parents=True)
    (gemini_home / "settings.json").write_text(
        '{"security":{"auth":{"selectedType":"oauth-personal"}}}'
    )
    accounts = gemini_home / "google_accounts.json"
    accounts.write_text('{"active":"user@example.com","old":[]}')

    result = check_gemini_auth()

    assert result.status == AuthStatus.AUTHENTICATED
    assert result.auth_file == accounts


def test_check_gemini_auth_selected_type_gemini_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod, "find_gemini_cli", lambda: "/usr/bin/gemini")
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    gemini_home = tmp_path / ".gemini"
    gemini_home.mkdir(parents=True)
    settings = gemini_home / "settings.json"
    settings.write_text('{"security":{"auth":{"selectedType":"gemini-api-key"}}}')

    result = check_gemini_auth()

    assert result.status == AuthStatus.AUTHENTICATED
    assert result.auth_file == settings


def test_check_gemini_auth_ductor_config_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod, "find_gemini_cli", lambda: "/usr/bin/gemini")
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    ductor_config = tmp_path / ".ductor" / "config" / "config.json"
    ductor_config.parent.mkdir(parents=True)
    ductor_config.write_text('{"gemini_api_key":"from-ductor-config"}')

    result = check_gemini_auth()

    assert result.status == AuthStatus.AUTHENTICATED
    assert result.auth_file == ductor_config


def test_check_gemini_auth_ductor_config_null_string_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod, "find_gemini_cli", lambda: "/usr/bin/gemini")
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    ductor_config = tmp_path / ".ductor" / "config" / "config.json"
    ductor_config.parent.mkdir(parents=True)
    ductor_config.write_text('{"gemini_api_key":"null"}')

    result = check_gemini_auth()

    assert result.status == AuthStatus.INSTALLED
    assert result.auth_file is None


def test_gemini_uses_api_key_mode_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    gemini_home = tmp_path / ".gemini"
    gemini_home.mkdir(parents=True)
    (gemini_home / "settings.json").write_text(
        '{"security":{"auth":{"selectedType":"gemini-api-key"}}}'
    )

    assert gemini_uses_api_key_mode() is True


def test_gemini_uses_api_key_mode_false_for_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    gemini_home = tmp_path / ".gemini"
    gemini_home.mkdir(parents=True)
    (gemini_home / "settings.json").write_text(
        '{"security":{"auth":{"selectedType":"oauth-personal"}}}'
    )

    assert gemini_uses_api_key_mode() is False


# -- Antigravity auth --


def test_check_antigravity_auth_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod.shutil, "which", lambda _cmd: None)

    result = check_antigravity_auth()

    assert result.provider == "antigravity"
    assert result.status == AuthStatus.NOT_FOUND


def test_check_antigravity_auth_installed_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod.shutil, "which", lambda _cmd: "C:/agy/bin/agy.exe")
    monkeypatch.setattr(_auth_mod, "_antigravity_cli_logged_in", lambda: False)

    result = check_antigravity_auth()

    assert result.status == AuthStatus.INSTALLED


def test_check_antigravity_auth_authenticated_via_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ductor_bot.cli.auth as _auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod.shutil, "which", lambda _cmd: "C:/agy/bin/agy.exe")
    monkeypatch.setattr(_auth_mod, "_antigravity_cli_logged_in", lambda: True)

    result = check_antigravity_auth()

    assert result.status == AuthStatus.AUTHENTICATED


def test_check_antigravity_auth_ccs_settings_are_only_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ductor_bot.cli.auth as _auth_mod

    settings = tmp_path / ".ccs" / "agy.settings.json"
    settings.parent.mkdir()
    settings.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_auth_mod.shutil, "which", lambda _cmd: None)

    result = check_antigravity_auth()

    assert result.status == AuthStatus.INSTALLED


def test_antigravity_cli_logged_in_returns_true_for_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    import ductor_bot.cli.auth as _auth_mod

    class _FakeResult:
        returncode = 0
        stdout = "claude-sonnet-4-5\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _FakeResult())

    assert _auth_mod._antigravity_cli_logged_in() is True


def test_antigravity_cli_logged_in_returns_false_for_login_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    import ductor_bot.cli.auth as _auth_mod

    class _FakeResult:
        returncode = 1
        stdout = ""
        stderr = "Please sign in to view available models"

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _FakeResult())

    assert _auth_mod._antigravity_cli_logged_in() is False


def test_antigravity_cli_logged_in_returns_false_on_probe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    import ductor_bot.cli.auth as _auth_mod

    def _raise(*_a: object, **_kw: object) -> None:
        raise subprocess.TimeoutExpired(["agy", "models"], timeout=10)

    monkeypatch.setattr(subprocess, "run", _raise)

    assert _auth_mod._antigravity_cli_logged_in() is False
