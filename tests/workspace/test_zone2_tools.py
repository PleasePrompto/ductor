"""Test that framework tools in cron_tools/ and webhook_tools/ are Zone 2."""

from pathlib import Path

import pytest

from ductor_bot.workspace.init import _walk_and_copy


@pytest.fixture
def temp_workspace(tmp_path: Path):
    """Create a temporary workspace structure."""
    home_defaults = tmp_path / "home_defaults"
    ductor_home = tmp_path / "ductor_home"

    # Create framework tool structure
    cron_tools = home_defaults / "workspace" / "tools" / "cron_tools"
    webhook_tools = home_defaults / "workspace" / "tools" / "webhook_tools"
    user_tools = home_defaults / "workspace" / "tools" / "user_tools"

    for d in [cron_tools, webhook_tools, user_tools]:
        d.mkdir(parents=True)

    return home_defaults, ductor_home


def test_cron_tools_py_files_are_zone2(temp_workspace):
    """Test that .py files in tools/cron_tools/ are always overwritten (Zone 2)."""
    home_defaults, ductor_home = temp_workspace

    # Create initial tool file
    cron_tools = home_defaults / "workspace" / "tools" / "cron_tools"
    tool_file = cron_tools / "cron_add.py"
    tool_file.write_text("# version 1")

    # First sync - should seed the file
    _walk_and_copy(home_defaults, ductor_home)

    deployed_tool = ductor_home / "workspace" / "tools" / "cron_tools" / "cron_add.py"
    assert deployed_tool.exists()
    assert deployed_tool.read_text() == "# version 1"

    # Update source file
    tool_file.write_text("# version 2 - UPDATED")

    # Second sync - should overwrite (Zone 2 behavior)
    _walk_and_copy(home_defaults, ductor_home)

    # File should be updated
    assert deployed_tool.read_text() == "# version 2 - UPDATED"


def test_webhook_tools_py_files_are_zone2(temp_workspace):
    """Test that .py files in tools/webhook_tools/ are always overwritten (Zone 2)."""
    home_defaults, ductor_home = temp_workspace

    # Create initial tool file
    webhook_tools = home_defaults / "workspace" / "tools" / "webhook_tools"
    tool_file = webhook_tools / "webhook_add.py"
    tool_file.write_text("# version 1")

    # First sync
    _walk_and_copy(home_defaults, ductor_home)

    deployed_tool = ductor_home / "workspace" / "tools" / "webhook_tools" / "webhook_add.py"
    assert deployed_tool.exists()
    assert deployed_tool.read_text() == "# version 1"

    # Update source
    tool_file.write_text("# version 2 - UPDATED")

    # Second sync - should overwrite
    _walk_and_copy(home_defaults, ductor_home)

    # File should be updated
    assert deployed_tool.read_text() == "# version 2 - UPDATED"


def test_user_tools_py_files_are_zone3(temp_workspace):
    """Test that .py files in tools/user_tools/ are NOT overwritten (Zone 3)."""
    home_defaults, ductor_home = temp_workspace

    # Create initial user tool
    user_tools = home_defaults / "workspace" / "tools" / "user_tools"
    tool_file = user_tools / "custom_tool.py"
    tool_file.write_text("# version 1")

    # First sync - should seed
    _walk_and_copy(home_defaults, ductor_home)

    deployed_tool = ductor_home / "workspace" / "tools" / "user_tools" / "custom_tool.py"
    assert deployed_tool.exists()
    assert deployed_tool.read_text() == "# version 1"

    # User modifies the file
    deployed_tool.write_text("# user's custom version")

    # Update source (framework tries to update)
    tool_file.write_text("# version 2 - FRAMEWORK UPDATE")

    # Second sync - should NOT overwrite (Zone 3 behavior)
    _walk_and_copy(home_defaults, ductor_home)

    # File should still have user's version
    assert deployed_tool.read_text() == "# user's custom version"


def test_non_py_files_in_tool_dirs_are_zone3(temp_workspace):
    """Test that non-.py files in framework tool dirs are still Zone 3."""
    home_defaults, ductor_home = temp_workspace

    # Create a non-.py file in cron_tools
    cron_tools = home_defaults / "workspace" / "tools" / "cron_tools"
    config_file = cron_tools / "config.json"
    config_file.write_text('{"version": 1}')

    # First sync
    _walk_and_copy(home_defaults, ductor_home)

    deployed_config = ductor_home / "workspace" / "tools" / "cron_tools" / "config.json"
    assert deployed_config.exists()
    assert deployed_config.read_text() == '{"version": 1}'

    # User modifies the config
    deployed_config.write_text('{"version": 1, "user_setting": true}')

    # Update source
    config_file.write_text('{"version": 2}')

    # Second sync - should NOT overwrite non-.py files (Zone 3)
    _walk_and_copy(home_defaults, ductor_home)

    # Should keep user's version
    assert deployed_config.read_text() == '{"version": 1, "user_setting": true}'


def test_shared_py_is_also_zone2(temp_workspace):
    """Test that _shared.py in tool dirs is also Zone 2 (framework file)."""
    home_defaults, ductor_home = temp_workspace

    # Create _shared.py
    cron_tools = home_defaults / "workspace" / "tools" / "cron_tools"
    shared_file = cron_tools / "_shared.py"
    shared_file.write_text("# shared utils v1")

    # First sync
    _walk_and_copy(home_defaults, ductor_home)

    deployed_shared = ductor_home / "workspace" / "tools" / "cron_tools" / "_shared.py"
    assert deployed_shared.read_text() == "# shared utils v1"

    # Update source
    shared_file.write_text("# shared utils v2 - UPDATED")

    # Second sync - should overwrite
    _walk_and_copy(home_defaults, ductor_home)

    # Should be updated
    assert deployed_shared.read_text() == "# shared utils v2 - UPDATED"
