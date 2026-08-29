"""Tests for persona discovery."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ductor_bot.personas.catalog import config_dir, is_known, load_personas


def _agent(base: Path, name: str, description: str = "does things") -> None:
    (base / "agents").mkdir(parents=True, exist_ok=True)
    (base / "agents" / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n"
    )


def test_no_agents_directory_is_not_an_error(tmp_path: Path) -> None:
    """Most installations have no personas; the feature simply stays invisible."""
    assert load_personas(tmp_path) == []


def test_personas_are_discovered_and_sorted(tmp_path: Path) -> None:
    for name in ("scout", "coder", "designer"):
        _agent(tmp_path, name)
    assert [p.name for p in load_personas(tmp_path)] == ["coder", "designer", "scout"]


def test_description_is_read_from_frontmatter(tmp_path: Path) -> None:
    _agent(tmp_path, "coder", "Code, debugging, review: infra and deployment")
    persona = load_personas(tmp_path)[0]
    assert persona.description == "Code, debugging, review: infra and deployment"


def test_frontmatter_name_wins_over_filename(tmp_path: Path) -> None:
    (tmp_path / "agents").mkdir(parents=True)
    (tmp_path / "agents" / "file-name.md").write_text("---\nname: real-name\n---\n")
    assert [p.name for p in load_personas(tmp_path)] == ["real-name"]


def test_file_without_frontmatter_still_loads(tmp_path: Path) -> None:
    (tmp_path / "agents").mkdir(parents=True)
    (tmp_path / "agents" / "bare.md").write_text("no frontmatter here\n")
    assert [p.name for p in load_personas(tmp_path)] == ["bare"]


def test_non_markdown_files_are_ignored(tmp_path: Path) -> None:
    _agent(tmp_path, "coder")
    (tmp_path / "agents" / "notes.txt").write_text("not an agent")
    assert [p.name for p in load_personas(tmp_path)] == ["coder"]


def test_is_known(tmp_path: Path) -> None:
    _agent(tmp_path, "coder")
    assert is_known("coder", tmp_path)
    assert not is_known("nope", tmp_path)


def test_config_dir_follows_the_env_var(tmp_path: Path) -> None:
    """Must match the CLI, which relocates everything under CLAUDE_CONFIG_DIR."""
    with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(tmp_path)}):
        assert config_dir() == tmp_path
