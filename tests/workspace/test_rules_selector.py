"""Tests for RulesSelector: provider-specific rule file deployment."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.workspace.paths import DuctorPaths
from ductor_bot.workspace.rules_selector import RulesSelector


@pytest.fixture
def mock_paths(tmp_path: Path) -> DuctorPaths:
    """Create DuctorPaths with temp directories for testing."""
    home_defaults = tmp_path / "home_defaults"
    ductor_home = tmp_path / "ductor_home"

    # Create directory structure
    home_defaults.mkdir()
    ductor_home.mkdir()

    # Create mock template directories
    config_dir = home_defaults / "config"
    cron_dir = home_defaults / "workspace" / "cron_tasks"
    webhook_dir = home_defaults / "workspace" / "tools" / "webhook_tools"

    for d in [config_dir, cron_dir, webhook_dir]:
        d.mkdir(parents=True)

    # Create all 3 template variants for each directory
    for d in [config_dir, cron_dir, webhook_dir]:
        (d / "RULES-claude-only.md").write_text("# Claude Only Template")
        (d / "RULES-codex-only.md").write_text("# Codex Only Template")
        (d / "RULES-claude-and-codex.md").write_text("# Claude and Codex Template")

    paths = MagicMock(spec=DuctorPaths)
    paths.home_defaults = home_defaults
    paths.ductor_home = ductor_home

    return paths


def test_variant_selection_claude_only(mock_paths: DuctorPaths) -> None:
    """Test variant selection when only Claude is authenticated."""
    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED),
        "codex": AuthResult(provider="codex", status=AuthStatus.NOT_FOUND),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        assert selector.get_variant_suffix() == "claude-only"


def test_variant_selection_codex_only(mock_paths: DuctorPaths) -> None:
    """Test variant selection when only Codex is authenticated."""
    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.NOT_FOUND),
        "codex": AuthResult(provider="codex", status=AuthStatus.AUTHENTICATED),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        assert selector.get_variant_suffix() == "codex-only"


def test_variant_selection_both(mock_paths: DuctorPaths) -> None:
    """Test variant selection when both CLIs are authenticated."""
    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED),
        "codex": AuthResult(provider="codex", status=AuthStatus.AUTHENTICATED),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        assert selector.get_variant_suffix() == "claude-and-codex"


def test_template_discovery(mock_paths: DuctorPaths) -> None:
    """Test automatic discovery of template directories."""
    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED),
        "codex": AuthResult(provider="codex", status=AuthStatus.NOT_FOUND),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        dirs = selector.discover_template_directories()

        # Should find 3 directories (config, cron_tasks, webhook_tools)
        assert len(dirs) == 3
        dir_names = {d.name for d in dirs}
        assert "config" in dir_names
        assert "cron_tasks" in dir_names
        assert "webhook_tools" in dir_names


def test_deploy_claude_only_no_agents_md(mock_paths: DuctorPaths) -> None:
    """Test that only CLAUDE.md is created when only Claude is authenticated."""
    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED),
        "codex": AuthResult(provider="codex", status=AuthStatus.NOT_FOUND),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        selector.deploy_rules()

        # Check that CLAUDE.md was deployed
        config_claude = mock_paths.ductor_home / "config" / "CLAUDE.md"
        assert config_claude.exists()
        assert "Claude Only Template" in config_claude.read_text()

        # Check that AGENTS.md was NOT created
        config_agents = mock_paths.ductor_home / "config" / "AGENTS.md"
        assert not config_agents.exists()


def test_deploy_codex_only_with_agents_md(mock_paths: DuctorPaths) -> None:
    """Test that only AGENTS.md is created when only Codex is authenticated."""
    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.NOT_FOUND),
        "codex": AuthResult(provider="codex", status=AuthStatus.AUTHENTICATED),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        selector.deploy_rules()

        # Check that AGENTS.md was deployed
        config_agents = mock_paths.ductor_home / "config" / "AGENTS.md"
        assert config_agents.exists()
        assert "Codex Only Template" in config_agents.read_text()

        # Check that CLAUDE.md was NOT created
        config_claude = mock_paths.ductor_home / "config" / "CLAUDE.md"
        assert not config_claude.exists()


def test_deploy_both_with_both_files(mock_paths: DuctorPaths) -> None:
    """Test that both CLAUDE.md and AGENTS.md are created when both CLIs are authenticated."""
    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED),
        "codex": AuthResult(provider="codex", status=AuthStatus.AUTHENTICATED),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        selector.deploy_rules()

        # Check that both files were deployed with same content
        config_claude = mock_paths.ductor_home / "config" / "CLAUDE.md"
        config_agents = mock_paths.ductor_home / "config" / "AGENTS.md"

        assert config_claude.exists()
        assert config_agents.exists()

        claude_content = config_claude.read_text()
        agents_content = config_agents.read_text()

        assert "Claude and Codex Template" in claude_content
        assert "Claude and Codex Template" in agents_content
        assert claude_content == agents_content  # Same content


def test_deploy_all_directories(mock_paths: DuctorPaths) -> None:
    """Test that all discovered directories get rules deployed."""
    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED),
        "codex": AuthResult(provider="codex", status=AuthStatus.AUTHENTICATED),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        selector.deploy_rules()

        # Check that all 3 directories got both files
        assert (mock_paths.ductor_home / "config" / "CLAUDE.md").exists()
        assert (mock_paths.ductor_home / "config" / "AGENTS.md").exists()
        assert (mock_paths.ductor_home / "workspace" / "cron_tasks" / "CLAUDE.md").exists()
        assert (mock_paths.ductor_home / "workspace" / "cron_tasks" / "AGENTS.md").exists()
        assert (
            mock_paths.ductor_home / "workspace" / "tools" / "webhook_tools" / "CLAUDE.md"
        ).exists()
        assert (
            mock_paths.ductor_home / "workspace" / "tools" / "webhook_tools" / "AGENTS.md"
        ).exists()


def test_fallback_to_static_template(mock_paths: DuctorPaths) -> None:
    """Test fallback to static RULES.md when no variants exist."""
    # Create directory with only static template
    static_dir = mock_paths.home_defaults / "static_test"
    static_dir.mkdir()
    (static_dir / "RULES.md").write_text("# Static Template")

    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED),
        "codex": AuthResult(provider="codex", status=AuthStatus.NOT_FOUND),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        selector.deploy_rules()

        # Should deploy static template as CLAUDE.md
        deployed = mock_paths.ductor_home / "static_test" / "CLAUDE.md"
        assert deployed.exists()
        assert "Static Template" in deployed.read_text()


def test_skip_directory_without_templates(mock_paths: DuctorPaths) -> None:
    """Test that directories without templates are skipped."""
    # Create directory without any templates
    empty_dir = mock_paths.home_defaults / "empty"
    empty_dir.mkdir()

    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED),
        "codex": AuthResult(provider="codex", status=AuthStatus.NOT_FOUND),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        dirs = selector.discover_template_directories()

        # empty_dir should not be in discovered directories
        assert empty_dir not in dirs


def test_cleanup_removes_agents_md_when_only_claude(mock_paths: DuctorPaths) -> None:
    """Test that stale AGENTS.md files are removed when only Claude is authenticated.

    Files inside workspace/cron_tasks/ are user-owned and must NOT be deleted.
    """
    # Pre-create old AGENTS.md files (simulating previous Codex installation)
    old_agents1 = mock_paths.ductor_home / "config" / "AGENTS.md"
    # cron_tasks files are user-owned — they must survive cleanup
    cron_task_agents = mock_paths.ductor_home / "workspace" / "cron_tasks" / "my-task" / "AGENTS.md"
    old_agents1.parent.mkdir(parents=True, exist_ok=True)
    cron_task_agents.parent.mkdir(parents=True, exist_ok=True)
    old_agents1.write_text("# Old Agents File")
    cron_task_agents.write_text("# User-owned cron task rules")

    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED),
        "codex": AuthResult(provider="codex", status=AuthStatus.NOT_FOUND),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        selector.deploy_rules()

        # Non-cron AGENTS.md should be removed
        assert not old_agents1.exists()
        # User-owned cron task AGENTS.md must be preserved
        assert cron_task_agents.exists()

        # CLAUDE.md files should exist
        assert (mock_paths.ductor_home / "config" / "CLAUDE.md").exists()


def test_cleanup_removes_claude_md_when_only_codex(mock_paths: DuctorPaths) -> None:
    """Test that stale CLAUDE.md files are removed when only Codex is authenticated.

    Files inside workspace/cron_tasks/ are user-owned and must NOT be deleted.
    """
    old_claude1 = mock_paths.ductor_home / "config" / "CLAUDE.md"
    cron_task_claude = mock_paths.ductor_home / "workspace" / "cron_tasks" / "my-task" / "CLAUDE.md"
    old_claude1.parent.mkdir(parents=True, exist_ok=True)
    cron_task_claude.parent.mkdir(parents=True, exist_ok=True)
    old_claude1.write_text("# Old Claude File")
    cron_task_claude.write_text("# User-owned cron task rules")

    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.NOT_FOUND),
        "codex": AuthResult(provider="codex", status=AuthStatus.AUTHENTICATED),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        selector.deploy_rules()

        # Non-cron CLAUDE.md should be removed
        assert not old_claude1.exists()
        # User-owned cron task CLAUDE.md must be preserved
        assert cron_task_claude.exists()

        # AGENTS.md files should exist
        assert (mock_paths.ductor_home / "config" / "AGENTS.md").exists()


def test_cleanup_keeps_both_when_both_authenticated(mock_paths: DuctorPaths) -> None:
    """Test that no cleanup happens when both CLIs are authenticated."""
    auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED),
        "codex": AuthResult(provider="codex", status=AuthStatus.AUTHENTICATED),
    }

    with patch("ductor_bot.workspace.rules_selector.check_all_auth", return_value=auth):
        selector = RulesSelector(mock_paths)
        selector.deploy_rules()

        # Both files should exist and stay
        config_claude = mock_paths.ductor_home / "config" / "CLAUDE.md"
        config_agents = mock_paths.ductor_home / "config" / "AGENTS.md"

        assert config_claude.exists()
        assert config_agents.exists()
