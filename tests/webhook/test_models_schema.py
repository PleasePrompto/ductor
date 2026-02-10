"""Test WebhookEntry schema extensions."""

from __future__ import annotations

import json
from pathlib import Path

from ductor_bot.webhook.models import WebhookEntry


def test_webhook_entry_new_fields_defaults() -> None:
    """WebhookEntry should accept new fields with None/empty defaults."""
    entry = WebhookEntry(
        id="test-1",
        title="Test Webhook",
        description="Test description",
        mode="wake",
        prompt_template="Test prompt",
    )

    # New fields should have None or empty defaults
    assert entry.provider is None
    assert entry.model is None
    assert entry.reasoning_effort is None
    assert entry.cli_parameters == []


def test_webhook_entry_new_fields_with_values() -> None:
    """WebhookEntry should accept and store new field values."""
    entry = WebhookEntry(
        id="test-2",
        title="Test Webhook",
        description="Test description",
        mode="cron_task",
        prompt_template="Test prompt",
        provider="codex",
        model="gpt-5.2-codex",
        reasoning_effort="high",
        cli_parameters=["--fast", "--verbose"],
    )

    assert entry.provider == "codex"
    assert entry.model == "gpt-5.2-codex"
    assert entry.reasoning_effort == "high"
    assert entry.cli_parameters == ["--fast", "--verbose"]


def test_webhook_entry_to_dict_includes_new_fields() -> None:
    """WebhookEntry.to_dict() should include new fields."""
    entry = WebhookEntry(
        id="test-3",
        title="Test Webhook",
        description="Test description",
        mode="wake",
        prompt_template="Test prompt",
        provider="codex",
        model="gpt-5.2-codex",
        reasoning_effort="high",
        cli_parameters=["--fast"],
    )

    data = entry.to_dict()

    assert data["provider"] == "codex"
    assert data["model"] == "gpt-5.2-codex"
    assert data["reasoning_effort"] == "high"
    assert data["cli_parameters"] == ["--fast"]


def test_webhook_entry_to_dict_with_none_values() -> None:
    """WebhookEntry.to_dict() should handle None values correctly."""
    entry = WebhookEntry(
        id="test-4",
        title="Test Webhook",
        description="Test description",
        mode="wake",
        prompt_template="Test prompt",
    )

    data = entry.to_dict()

    # None values should be included as None (not omitted)
    assert "provider" in data
    assert data["provider"] is None
    assert "model" in data
    assert data["model"] is None
    assert "reasoning_effort" in data
    assert data["reasoning_effort"] is None
    assert "cli_parameters" in data
    assert data["cli_parameters"] == []


def test_webhook_entry_from_dict_with_new_fields() -> None:
    """WebhookEntry.from_dict() should deserialize new fields."""
    data = {
        "id": "test-5",
        "title": "Test Webhook",
        "description": "Test description",
        "mode": "cron_task",
        "prompt_template": "Test prompt",
        "provider": "codex",
        "model": "gpt-5.2-codex",
        "reasoning_effort": "high",
        "cli_parameters": ["--fast", "--verbose"],
    }

    entry = WebhookEntry.from_dict(data)

    assert entry.provider == "codex"
    assert entry.model == "gpt-5.2-codex"
    assert entry.reasoning_effort == "high"
    assert entry.cli_parameters == ["--fast", "--verbose"]


def test_webhook_entry_from_dict_backward_compatibility() -> None:
    """WebhookEntry.from_dict() should handle old JSON without new fields."""
    old_data = {
        "id": "test-6",
        "title": "Test Webhook",
        "description": "Test description",
        "mode": "wake",
        "prompt_template": "Test prompt",
        "enabled": True,
    }

    # Should not raise, should use defaults
    entry = WebhookEntry.from_dict(old_data)

    assert entry.provider is None
    assert entry.model is None
    assert entry.reasoning_effort is None
    assert entry.cli_parameters == []


def test_webhook_entry_round_trip_serialization() -> None:
    """WebhookEntry should serialize and deserialize without data loss."""
    original = WebhookEntry(
        id="test-7",
        title="Test Webhook",
        description="Test description",
        mode="cron_task",
        prompt_template="Test prompt",
        provider="codex",
        model="gpt-5.2-codex",
        reasoning_effort="high",
        cli_parameters=["--fast", "--verbose"],
    )

    # Serialize
    data = original.to_dict()

    # Deserialize
    restored = WebhookEntry.from_dict(data)

    assert restored.id == original.id
    assert restored.provider == original.provider
    assert restored.model == original.model
    assert restored.reasoning_effort == original.reasoning_effort
    assert restored.cli_parameters == original.cli_parameters


def test_webhook_entry_json_round_trip(tmp_path: Path) -> None:
    """WebhookEntry should persist correctly to JSON file."""
    json_path = tmp_path / "webhook.json"

    entry = WebhookEntry(
        id="test-8",
        title="Test Webhook",
        description="Test description",
        mode="wake",
        prompt_template="Test prompt",
        provider="codex",
        model="gpt-5.2-codex",
        reasoning_effort="high",
        cli_parameters=["--fast"],
    )

    # Write to file
    data = entry.to_dict()
    json_path.write_text(json.dumps(data, indent=2))

    # Read back
    loaded_data = json.loads(json_path.read_text())
    loaded_entry = WebhookEntry.from_dict(loaded_data)

    assert loaded_entry.provider == "codex"
    assert loaded_entry.model == "gpt-5.2-codex"
    assert loaded_entry.reasoning_effort == "high"
    assert loaded_entry.cli_parameters == ["--fast"]


def test_empty_cli_parameters_persists_as_empty_list() -> None:
    """Empty cli_parameters should persist as [], not None."""
    entry = WebhookEntry(
        id="test-9",
        title="Test Webhook",
        description="Test description",
        mode="wake",
        prompt_template="Test prompt",
        cli_parameters=[],
    )

    # Serialize
    data = entry.to_dict()

    assert "cli_parameters" in data
    assert data["cli_parameters"] == []
    assert isinstance(data["cli_parameters"], list)

    # Round-trip
    restored = WebhookEntry.from_dict(data)

    assert restored.cli_parameters == []
    assert isinstance(restored.cli_parameters, list)


def test_webhook_entry_with_all_execution_overrides() -> None:
    """WebhookEntry should support all execution override fields together."""
    entry = WebhookEntry(
        id="test-10",
        title="Full Override Test",
        description="Testing all override fields",
        mode="cron_task",
        prompt_template="{{action}} in {{repo}}",
        task_folder="tasks/webhook/",
        provider="codex",
        model="gpt-5.1-codex-mini",
        reasoning_effort="low",
        cli_parameters=["--no-cache", "--debug"],
    )

    data = entry.to_dict()

    # All overrides should be present
    assert data["provider"] == "codex"
    assert data["model"] == "gpt-5.1-codex-mini"
    assert data["reasoning_effort"] == "low"
    assert data["cli_parameters"] == ["--no-cache", "--debug"]

    # Other fields should be preserved
    assert data["mode"] == "cron_task"
    assert data["task_folder"] == "tasks/webhook/"
    assert data["prompt_template"] == "{{action}} in {{repo}}"

    # Round-trip should preserve everything
    restored = WebhookEntry.from_dict(data)
    assert restored.provider == entry.provider
    assert restored.model == entry.model
    assert restored.reasoning_effort == entry.reasoning_effort
    assert restored.cli_parameters == entry.cli_parameters
