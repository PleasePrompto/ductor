"""Tests for webhook data models and template rendering."""

from __future__ import annotations

from typing import Any

from ductor_bot.webhook.models import WebhookEntry, WebhookResult, render_template

# ---------------------------------------------------------------------------
# WebhookEntry
# ---------------------------------------------------------------------------


def _make_entry(**overrides: Any) -> WebhookEntry:
    defaults: dict[str, Any] = {
        "id": "email-notify",
        "title": "Neue Emails",
        "description": "Zapier pingt bei eingehenden Emails",
        "mode": "wake",
        "prompt_template": "Neue Email von {{from}}: {{subject}}",
    }
    defaults.update(overrides)
    return WebhookEntry(**defaults)


class TestWebhookEntry:
    def test_to_dict(self) -> None:
        entry = _make_entry()
        d = entry.to_dict()
        assert d["id"] == "email-notify"
        assert d["mode"] == "wake"
        assert d["enabled"] is True
        assert d["trigger_count"] == 0

    def test_from_dict(self) -> None:
        data = {
            "id": "test",
            "title": "Test",
            "description": "desc",
            "mode": "cron_task",
            "prompt_template": "do {{action}}",
            "enabled": False,
            "task_folder": "test-folder",
            "created_at": "2025-01-01T00:00:00Z",
            "trigger_count": 5,
            "last_triggered_at": "2025-06-01T12:00:00Z",
            "last_error": "error:timeout",
        }
        entry = WebhookEntry.from_dict(data)
        assert entry.id == "test"
        assert entry.enabled is False
        assert entry.task_folder == "test-folder"
        assert entry.trigger_count == 5
        assert entry.last_error == "error:timeout"

    def test_from_dict_defaults(self) -> None:
        data = {
            "id": "min",
            "title": "Min",
            "mode": "wake",
            "prompt_template": "go",
        }
        entry = WebhookEntry.from_dict(data)
        assert entry.enabled is True
        assert entry.task_folder is None
        assert entry.trigger_count == 0
        assert entry.last_triggered_at is None
        assert entry.last_error is None
        assert entry.description == ""

    def test_auto_created_at(self) -> None:
        entry = _make_entry()
        assert entry.created_at != ""

    def test_roundtrip(self) -> None:
        original = _make_entry(task_folder="my-task", trigger_count=3)
        rebuilt = WebhookEntry.from_dict(original.to_dict())
        assert rebuilt.id == original.id
        assert rebuilt.title == original.title
        assert rebuilt.mode == original.mode
        assert rebuilt.task_folder == original.task_folder
        assert rebuilt.trigger_count == original.trigger_count

    def test_auth_fields_defaults(self) -> None:
        entry = _make_entry()
        assert entry.auth_mode == "bearer"
        assert entry.token == ""
        assert entry.hmac_secret == ""
        assert entry.hmac_header == ""

    def test_hmac_config_fields_defaults(self) -> None:
        entry = _make_entry()
        assert entry.hmac_algorithm == "sha256"
        assert entry.hmac_encoding == "hex"
        assert entry.hmac_sig_prefix == "sha256="
        assert entry.hmac_sig_regex == ""
        assert entry.hmac_payload_prefix_regex == ""

    def test_to_dict_includes_auth_fields(self) -> None:
        entry = _make_entry(token="my-token", auth_mode="bearer")
        d = entry.to_dict()
        assert d["auth_mode"] == "bearer"
        assert d["token"] == "my-token"
        assert d["hmac_secret"] == ""
        assert d["hmac_header"] == ""

    def test_to_dict_includes_hmac_config_fields(self) -> None:
        entry = _make_entry(
            auth_mode="hmac",
            hmac_algorithm="sha1",
            hmac_encoding="base64",
            hmac_sig_prefix="",
            hmac_sig_regex=r"v1=([a-f0-9]+)",
            hmac_payload_prefix_regex=r"t=(\d+)",
        )
        d = entry.to_dict()
        assert d["hmac_algorithm"] == "sha1"
        assert d["hmac_encoding"] == "base64"
        assert d["hmac_sig_prefix"] == ""
        assert d["hmac_sig_regex"] == r"v1=([a-f0-9]+)"
        assert d["hmac_payload_prefix_regex"] == r"t=(\d+)"

    def test_from_dict_with_auth_fields(self) -> None:
        data = {
            "id": "hmac-hook",
            "title": "HMAC Hook",
            "mode": "wake",
            "prompt_template": "{{event}}",
            "auth_mode": "hmac",
            "token": "",
            "hmac_secret": "gh-secret-123",
            "hmac_header": "X-Hub-Signature-256",
        }
        entry = WebhookEntry.from_dict(data)
        assert entry.auth_mode == "hmac"
        assert entry.hmac_secret == "gh-secret-123"
        assert entry.hmac_header == "X-Hub-Signature-256"
        assert entry.token == ""

    def test_from_dict_with_hmac_config_fields(self) -> None:
        data = {
            "id": "stripe-hook",
            "title": "Stripe",
            "mode": "wake",
            "prompt_template": "{{type}}",
            "auth_mode": "hmac",
            "hmac_secret": "whsec_xxx",
            "hmac_header": "Stripe-Signature",
            "hmac_algorithm": "sha256",
            "hmac_encoding": "hex",
            "hmac_sig_prefix": "",
            "hmac_sig_regex": r"v1=([a-f0-9]+)",
            "hmac_payload_prefix_regex": r"t=(\d+)",
        }
        entry = WebhookEntry.from_dict(data)
        assert entry.hmac_sig_regex == r"v1=([a-f0-9]+)"
        assert entry.hmac_payload_prefix_regex == r"t=(\d+)"
        assert entry.hmac_sig_prefix == ""

    def test_from_dict_backward_compat_no_auth_fields(self) -> None:
        data = {
            "id": "legacy",
            "title": "Legacy",
            "mode": "wake",
            "prompt_template": "{{msg}}",
        }
        entry = WebhookEntry.from_dict(data)
        assert entry.auth_mode == "bearer"
        assert entry.token == ""
        assert entry.hmac_secret == ""
        assert entry.hmac_header == ""
        # New HMAC config fields also have safe defaults
        assert entry.hmac_algorithm == "sha256"
        assert entry.hmac_encoding == "hex"
        assert entry.hmac_sig_prefix == "sha256="
        assert entry.hmac_sig_regex == ""
        assert entry.hmac_payload_prefix_regex == ""

    def test_roundtrip_bearer_with_token(self) -> None:
        original = _make_entry(token="secret-abc", auth_mode="bearer")
        rebuilt = WebhookEntry.from_dict(original.to_dict())
        assert rebuilt.auth_mode == "bearer"
        assert rebuilt.token == "secret-abc"

    def test_roundtrip_hmac_hook(self) -> None:
        original = _make_entry(
            auth_mode="hmac",
            hmac_secret="stripe-whsec-123",
            hmac_header="Stripe-Signature",
        )
        rebuilt = WebhookEntry.from_dict(original.to_dict())
        assert rebuilt.auth_mode == "hmac"
        assert rebuilt.hmac_secret == "stripe-whsec-123"
        assert rebuilt.hmac_header == "Stripe-Signature"

    def test_roundtrip_hmac_configurable_fields(self) -> None:
        original = _make_entry(
            auth_mode="hmac",
            hmac_secret="whsec_test",
            hmac_header="Stripe-Signature",
            hmac_algorithm="sha256",
            hmac_encoding="hex",
            hmac_sig_prefix="",
            hmac_sig_regex=r"v1=([a-f0-9]+)",
            hmac_payload_prefix_regex=r"t=(\d+)",
        )
        rebuilt = WebhookEntry.from_dict(original.to_dict())
        assert rebuilt.hmac_algorithm == "sha256"
        assert rebuilt.hmac_encoding == "hex"
        assert rebuilt.hmac_sig_prefix == ""
        assert rebuilt.hmac_sig_regex == r"v1=([a-f0-9]+)"
        assert rebuilt.hmac_payload_prefix_regex == r"t=(\d+)"


# ---------------------------------------------------------------------------
# WebhookResult
# ---------------------------------------------------------------------------


class TestWebhookResult:
    def test_is_frozen(self) -> None:
        result = WebhookResult(
            hook_id="test",
            hook_title="Test",
            mode="wake",
            result_text="hello",
            status="success",
        )
        assert result.hook_id == "test"
        assert result.status == "success"


# ---------------------------------------------------------------------------
# render_template
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    def test_basic_replacement(self) -> None:
        result = render_template(
            "Email von {{from}}: {{subject}}",
            {"from": "alice@example.com", "subject": "Hello"},
        )
        assert result == "Email von alice@example.com: Hello"

    def test_missing_key_renders_placeholder(self) -> None:
        result = render_template("{{name}} sent {{message}}", {"name": "Bob"})
        assert result == "Bob sent {{?message}}"

    def test_no_placeholders(self) -> None:
        result = render_template("plain text", {"key": "val"})
        assert result == "plain text"

    def test_empty_payload(self) -> None:
        result = render_template("{{a}} and {{b}}", {})
        assert result == "{{?a}} and {{?b}}"

    def test_numeric_value(self) -> None:
        result = render_template("PR #{{number}}", {"number": 42})
        assert result == "PR #42"

    def test_empty_string_value(self) -> None:
        result = render_template("val={{x}}", {"x": ""})
        assert result == "val="

    def test_none_value_treated_as_missing(self) -> None:
        result = render_template("{{x}}", {"x": None})
        assert result == "{{?x}}"

    def test_repeated_placeholder(self) -> None:
        result = render_template("{{a}} {{a}}", {"a": "X"})
        assert result == "X X"
