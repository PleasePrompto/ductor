"""Tests for config hot-reload: diff, classify, and ConfigReloader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from ductor_bot.config import AgentConfig
from ductor_bot.config_reload import (
    ConfigReloader,
    classify_changes,
    diff_configs,
)


def _make_config(**overrides: Any) -> AgentConfig:
    return AgentConfig(**overrides)


class TestDiffConfigs:
    def test_no_changes(self) -> None:
        cfg = _make_config()
        assert diff_configs(cfg, cfg) == {}

    def test_detects_scalar_change(self) -> None:
        old = _make_config(model="sonnet")
        new = _make_config(model="opus")
        changes = diff_configs(old, new)
        assert "model" in changes
        assert changes["model"] == ("sonnet", "opus")

    def test_detects_multiple_changes(self) -> None:
        old = _make_config(model="sonnet", provider="claude")
        new = _make_config(model="opus", provider="codex")
        changes = diff_configs(old, new)
        assert len(changes) == 2
        assert "model" in changes
        assert "provider" in changes

    def test_detects_nested_change(self) -> None:
        old = _make_config()
        new = _make_config()
        new.streaming.min_chars = 999
        changes = diff_configs(old, new)
        assert "streaming" in changes

    def test_unchanged_fields_excluded(self) -> None:
        old = _make_config(model="sonnet")
        new = _make_config(model="opus")
        changes = diff_configs(old, new)
        assert "provider" not in changes
        assert "streaming" not in changes


class TestClassifyChanges:
    def test_hot_reloadable(self) -> None:
        changes = {"model": ("sonnet", "opus"), "reasoning_effort": ("low", "high")}
        hot, restart = classify_changes(changes)
        assert "model" in hot
        assert "reasoning_effort" in hot
        assert restart == []

    def test_restart_required(self) -> None:
        changes = {"telegram_token": ("old", "new"), "docker": ({}, {"enabled": True})}
        hot, restart = classify_changes(changes)
        assert hot == {}
        assert "telegram_token" in restart
        assert "docker" in restart

    def test_mixed(self) -> None:
        changes = {
            "model": ("sonnet", "opus"),
            "telegram_token": ("old", "new"),
        }
        hot, restart = classify_changes(changes)
        assert "model" in hot
        assert "telegram_token" in restart

    def test_unknown_field_requires_restart(self) -> None:
        changes = {"unknown_future_field": (None, "value")}
        _, restart = classify_changes(changes)
        assert "unknown_future_field" in restart

    def test_project_roots_is_hot_reloadable(self) -> None:
        changes = {"project_roots": ({}, {"my-project": "~/code/my-project"})}
        hot, restart = classify_changes(changes)
        assert hot == {"project_roots": {"my-project": "~/code/my-project"}}
        assert restart == []


class TestConfigReloader:
    def _write_config(self, path: Path, **overrides: Any) -> AgentConfig:
        cfg = _make_config(**overrides)
        data = cfg.model_dump(mode="json")
        path.write_text(json.dumps(data), encoding="utf-8")
        return cfg

    async def test_no_change_no_callback(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        cfg = self._write_config(config_path)

        on_hot = MagicMock()
        reloader = ConfigReloader(config_path, cfg, on_hot_reload=on_hot)

        await reloader._check()
        on_hot.assert_not_called()

    async def test_detects_change_and_calls_hot_reload(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        cfg = self._write_config(config_path, model="sonnet")

        on_hot = MagicMock()
        reloader = ConfigReloader(config_path, cfg, on_hot_reload=on_hot)

        # Mutate the file
        self._write_config(config_path, model="opus")

        await reloader._check()
        on_hot.assert_called_once()
        call_config, call_hot = on_hot.call_args[0]
        assert isinstance(call_config, AgentConfig)
        assert "model" in call_hot

    async def test_restart_callback(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        cfg = self._write_config(config_path)

        on_restart = MagicMock()
        reloader = ConfigReloader(config_path, cfg, on_restart_needed=on_restart)

        new_data = cfg.model_dump(mode="json")
        new_data["telegram_token"] = "new-token-value"
        config_path.write_text(json.dumps(new_data), encoding="utf-8")

        await reloader._check()
        on_restart.assert_called_once()
        fields = on_restart.call_args[0][0]
        assert "telegram_token" in fields

    async def test_invalid_json_no_crash(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        cfg = self._write_config(config_path)

        on_hot = MagicMock()
        reloader = ConfigReloader(config_path, cfg, on_hot_reload=on_hot)

        config_path.write_text("{invalid json", encoding="utf-8")

        await reloader._check()
        on_hot.assert_not_called()

    async def test_invalid_pydantic_no_crash(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        cfg = self._write_config(config_path)

        on_hot = MagicMock()
        reloader = ConfigReloader(config_path, cfg, on_hot_reload=on_hot)

        config_path.write_text(json.dumps({"log_level": 12345}), encoding="utf-8")

        await reloader._check()
        on_hot.assert_not_called()

    async def test_missing_file_no_crash(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        cfg = _make_config()

        reloader = ConfigReloader(config_path, cfg)
        await reloader._check()  # should not raise

    async def test_start_stop(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        cfg = self._write_config(config_path)

        reloader = ConfigReloader(config_path, cfg)
        await reloader.start()
        assert reloader._task is not None

        await reloader.stop()
        assert reloader._task is None

    async def test_double_start_idempotent(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        cfg = self._write_config(config_path)

        reloader = ConfigReloader(config_path, cfg)
        await reloader.start()
        task1 = reloader._task

        await reloader.start()
        assert reloader._task is task1

        await reloader.stop()

    async def test_apply_hot_mutates_config(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        cfg = self._write_config(config_path, model="sonnet")

        applied: dict[str, Any] = {}

        def capture(_config: AgentConfig, hot: dict[str, Any]) -> None:
            applied.update(hot)

        reloader = ConfigReloader(config_path, cfg, on_hot_reload=capture)

        self._write_config(config_path, model="opus")
        await reloader._check()

        assert cfg.model == "opus"
        assert applied.get("model") == "opus"

    async def test_project_roots_hot_reload(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        cfg = self._write_config(config_path)

        applied: dict[str, Any] = {}

        def capture(_config: AgentConfig, hot: dict[str, Any]) -> None:
            applied.update(hot)

        reloader = ConfigReloader(config_path, cfg, on_hot_reload=capture)

        self._write_config(config_path, project_roots={"my-project": "~/code/my-project"})
        await reloader._check()

        assert cfg.project_roots == {"my-project": "~/code/my-project"}
        assert applied.get("project_roots") == {"my-project": "~/code/my-project"}

    async def test_same_content_rewrite_no_callback(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        cfg = self._write_config(config_path, model="sonnet")

        on_hot = MagicMock()
        reloader = ConfigReloader(config_path, cfg, on_hot_reload=on_hot)

        # Rewrite with same content (mtime changes but no diff)
        self._write_config(config_path, model="sonnet")

        await reloader._check()
        on_hot.assert_not_called()
