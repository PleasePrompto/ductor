"""Tests for per-topic project root resolution."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.workspace.project_roots import resolve_project_root

if TYPE_CHECKING:
    import pytest


def test_topic_name_has_highest_priority(tmp_path: Path) -> None:
    by_name = tmp_path / "by-name"
    by_chat_topic = tmp_path / "by-chat-topic"
    by_topic = tmp_path / "by-topic"
    for d in (by_name, by_chat_topic, by_topic):
        d.mkdir()
    roots = {
        "my-project": str(by_name),
        "100:5": str(by_chat_topic),
        "5": str(by_topic),
    }
    result = resolve_project_root(roots, chat_id=100, topic_id=5, topic_name="my-project")
    assert result == str(by_name.resolve())


def test_chat_topic_key_beats_plain_topic_id(tmp_path: Path) -> None:
    by_chat_topic = tmp_path / "by-chat-topic"
    by_topic = tmp_path / "by-topic"
    by_chat_topic.mkdir()
    by_topic.mkdir()
    roots = {"100:5": str(by_chat_topic), "5": str(by_topic)}
    result = resolve_project_root(roots, chat_id=100, topic_id=5, topic_name=None)
    assert result == str(by_chat_topic.resolve())


def test_plain_topic_id_key(tmp_path: Path) -> None:
    by_topic = tmp_path / "by-topic"
    by_topic.mkdir()
    roots = {"5": str(by_topic)}
    result = resolve_project_root(roots, chat_id=100, topic_id=5, topic_name=None)
    assert result == str(by_topic.resolve())


def test_tilde_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "code" / "proj"
    project.mkdir(parents=True)
    roots = {"proj": "~/code/proj"}
    result = resolve_project_root(roots, chat_id=1, topic_id=7, topic_name="proj")
    assert result == str(project.resolve())


def test_missing_directory_warns_and_falls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    existing = tmp_path / "exists"
    existing.mkdir()
    roots = {
        "my-project": str(tmp_path / "does-not-exist"),
        "100:5": str(existing),
    }
    with caplog.at_level("WARNING", logger="ductor_bot.workspace.project_roots"):
        result = resolve_project_root(roots, chat_id=100, topic_id=5, topic_name="my-project")
    assert result == str(existing.resolve())
    assert any("does-not-exist" in rec.message for rec in caplog.records)


def test_all_candidates_missing_returns_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    roots = {"5": str(tmp_path / "gone")}
    with caplog.at_level("WARNING", logger="ductor_bot.workspace.project_roots"):
        result = resolve_project_root(roots, chat_id=100, topic_id=5, topic_name=None)
    assert result is None
    assert any("gone" in rec.message for rec in caplog.records)


def test_file_path_is_not_a_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-dir.txt"
    file_path.write_text("hi")
    roots = {"5": str(file_path)}
    assert resolve_project_root(roots, chat_id=1, topic_id=5, topic_name=None) is None


def test_topic_id_none_returns_none(tmp_path: Path) -> None:
    roots = {"general": str(tmp_path)}
    assert resolve_project_root(roots, chat_id=1, topic_id=None, topic_name="general") is None


def test_empty_roots_returns_none() -> None:
    assert resolve_project_root({}, chat_id=1, topic_id=5, topic_name="x") is None


def test_no_matching_key_returns_none(tmp_path: Path) -> None:
    roots = {"other": str(tmp_path)}
    assert resolve_project_root(roots, chat_id=1, topic_id=5, topic_name="mine") is None
