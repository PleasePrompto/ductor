"""Tests for WebhookManager: JSON-based hook storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ductor_bot.webhook.manager import WebhookManager
from ductor_bot.webhook.models import WebhookEntry


def _make_manager(tmp_path: Path) -> WebhookManager:
    hooks_path = tmp_path / "webhooks.json"
    return WebhookManager(hooks_path=hooks_path)


def _make_hook(hook_id: str = "email-notify", **overrides: Any) -> WebhookEntry:
    defaults: dict[str, Any] = {
        "id": hook_id,
        "title": "Test Hook",
        "description": "A test webhook",
        "mode": "wake",
        "prompt_template": "{{msg}}",
    }
    defaults.update(overrides)
    return WebhookEntry(**defaults)


# -- WebhookManager CRUD --


class TestWebhookManagerCRUD:
    def test_add_hook_saves_to_json(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_hook(_make_hook())

        data = json.loads((tmp_path / "webhooks.json").read_text())
        assert len(data["hooks"]) == 1
        assert data["hooks"][0]["id"] == "email-notify"

    def test_add_duplicate_raises(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_hook(_make_hook())
        with pytest.raises(ValueError, match="already exists"):
            mgr.add_hook(_make_hook())

    def test_remove_hook(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_hook(_make_hook())
        removed = mgr.remove_hook("email-notify")

        assert removed is True
        data = json.loads((tmp_path / "webhooks.json").read_text())
        assert len(data["hooks"]) == 0

    def test_remove_nonexistent_returns_false(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        assert mgr.remove_hook("nope") is False

    def test_list_hooks(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        for i in range(3):
            mgr.add_hook(_make_hook(f"hook-{i}"))

        hooks = mgr.list_hooks()
        assert len(hooks) == 3
        assert [h.id for h in hooks] == ["hook-0", "hook-1", "hook-2"]

    def test_get_hook(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_hook(_make_hook())

        found = mgr.get_hook("email-notify")
        assert found is not None
        assert found.title == "Test Hook"

    def test_get_nonexistent_returns_none(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        assert mgr.get_hook("nope") is None

    def test_update_hook(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_hook(_make_hook())

        updated = mgr.update_hook("email-notify", title="New Title", enabled=False)
        assert updated is True

        hook = mgr.get_hook("email-notify")
        assert hook is not None
        assert hook.title == "New Title"
        assert hook.enabled is False

    def test_update_nonexistent_returns_false(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        assert mgr.update_hook("nope", title="x") is False

    def test_record_trigger_success(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_hook(_make_hook())

        mgr.record_trigger("email-notify")
        hook = mgr.get_hook("email-notify")
        assert hook is not None
        assert hook.trigger_count == 1
        assert hook.last_triggered_at is not None
        assert hook.last_error is None

    def test_record_trigger_with_error(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_hook(_make_hook())

        mgr.record_trigger("email-notify", error="error:timeout")
        hook = mgr.get_hook("email-notify")
        assert hook is not None
        assert hook.trigger_count == 1
        assert hook.last_error == "error:timeout"

    def test_record_trigger_clears_error_on_success(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_hook(_make_hook())

        mgr.record_trigger("email-notify", error="error:timeout")
        mgr.record_trigger("email-notify")

        hook = mgr.get_hook("email-notify")
        assert hook is not None
        assert hook.trigger_count == 2
        assert hook.last_error is None

    def test_record_trigger_nonexistent_is_noop(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.record_trigger("nope")  # should not raise

    def test_reload_picks_up_external_changes(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_hook(_make_hook("original"))
        assert len(mgr.list_hooks()) == 1

        data = {
            "hooks": [
                _make_hook("original").to_dict(),
                _make_hook("external").to_dict(),
            ],
        }
        (tmp_path / "webhooks.json").write_text(json.dumps(data), encoding="utf-8")

        mgr.reload()
        assert len(mgr.list_hooks()) == 2
        assert mgr.get_hook("external") is not None


# -- Persistence --


class TestPersistence:
    def test_loads_from_existing_json(self, tmp_path: Path) -> None:
        hooks_path = tmp_path / "webhooks.json"
        data = {
            "hooks": [
                {
                    "id": "existing",
                    "title": "Existing",
                    "description": "Was saved before",
                    "mode": "wake",
                    "prompt_template": "{{msg}}",
                    "enabled": True,
                },
            ],
        }
        hooks_path.write_text(json.dumps(data))

        mgr = WebhookManager(hooks_path=hooks_path)
        hooks = mgr.list_hooks()
        assert len(hooks) == 1
        assert hooks[0].id == "existing"

    def test_handles_missing_json_file(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        assert mgr.list_hooks() == []

    def test_handles_corrupt_json_file(self, tmp_path: Path) -> None:
        hooks_path = tmp_path / "webhooks.json"
        hooks_path.write_text("not valid json{{{")

        mgr = WebhookManager(hooks_path=hooks_path)
        assert mgr.list_hooks() == []

    def test_atomic_save_creates_parent(self, tmp_path: Path) -> None:
        hooks_path = tmp_path / "subdir" / "webhooks.json"
        mgr = WebhookManager(hooks_path=hooks_path)
        mgr.add_hook(_make_hook())
        assert hooks_path.exists()

    def test_loads_legacy_hook_without_auth_fields(self, tmp_path: Path) -> None:
        hooks_path = tmp_path / "webhooks.json"
        data = {
            "hooks": [
                {
                    "id": "legacy",
                    "title": "Legacy",
                    "description": "Old hook",
                    "mode": "wake",
                    "prompt_template": "{{msg}}",
                },
            ],
        }
        hooks_path.write_text(json.dumps(data))
        mgr = WebhookManager(hooks_path=hooks_path)
        hook = mgr.get_hook("legacy")
        assert hook is not None
        assert hook.auth_mode == "bearer"
        assert hook.token == ""
        assert hook.hmac_secret == ""

    def test_auth_fields_persisted(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_hook(
            _make_hook(
                auth_mode="hmac",
                token="",
                hmac_secret="secret-123",
                hmac_header="X-Sig",
            )
        )
        data = json.loads((tmp_path / "webhooks.json").read_text())
        hook_data = data["hooks"][0]
        assert hook_data["auth_mode"] == "hmac"
        assert hook_data["hmac_secret"] == "secret-123"
        assert hook_data["hmac_header"] == "X-Sig"

    def test_update_hook_token(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_hook(_make_hook(token="old-token"))
        mgr.update_hook("email-notify", token="new-token")
        hook = mgr.get_hook("email-notify")
        assert hook is not None
        assert hook.token == "new-token"
