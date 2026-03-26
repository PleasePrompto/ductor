"""Tests for the Scopewise ticket intake router."""

from __future__ import annotations

import json
from pathlib import Path

from ductor_bot.factory.scopewise_ticket_router import (
    AuditTrail,
    BindingStore,
    GitHubIssue,
    NormalizedTicketEvent,
    route_event,
)


class FakeGitHub:
    """Small in-memory fake for router tests."""

    def __init__(self) -> None:
        self.issues: dict[int, GitHubIssue] = {}
        self.next_number = 100

    def seed(self, issue: GitHubIssue) -> None:
        self.issues[issue.number] = issue
        self.next_number = max(self.next_number, issue.number + 1)

    def get_issue(self, repo: str, issue_number: int) -> GitHubIssue:
        return self.issues[issue_number]

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> GitHubIssue:
        issue = GitHubIssue(
            number=self.next_number,
            url=f"https://github.com/{repo}/issues/{self.next_number}",
            state="open",
            title=title,
            body=body,
            labels=labels[:],
        )
        self.issues[issue.number] = issue
        self.next_number += 1
        return issue

    def update_issue(
        self,
        repo: str,
        issue_number: int,
        *,
        title: str,
        body: str,
        add_labels: list[str],
        remove_labels: list[str],
    ) -> GitHubIssue:
        issue = self.issues[issue_number]
        labels = [label for label in issue.labels if label not in remove_labels]
        for label in add_labels:
            if label not in labels:
                labels.append(label)
        updated = GitHubIssue(
            number=issue.number,
            url=issue.url,
            state=issue.state,
            title=title,
            body=body,
            labels=labels,
        )
        self.issues[issue_number] = updated
        return updated


def _event(payload: dict[str, object]) -> NormalizedTicketEvent:
    return NormalizedTicketEvent.from_payload(payload, github_repo_default="samhavens/scopewise")


def _audit_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_create_manual_issue_records_audit(tmp_path: Path) -> None:
    fake = FakeGitHub()
    bindings = BindingStore(tmp_path / "bindings.sqlite3")
    audit = AuditTrail(tmp_path / "audit.jsonl")
    payload = {
        "source": "manual",
        "owner": "ops",
        "summary": "Backfill Scopewise ticket intake router",
        "routing_reason": "Ops owns Ductor-side webhook and task plumbing.",
        "next_action": "Create the first narrow intake path.",
        "repo": "scopewise",
        "environment": "n/a",
        "evidence": {"links": ["https://example.test/spec"]},
    }
    event = _event(payload)

    result = route_event(
        event=event,
        raw_payload=payload,
        github=fake,
        bindings=bindings,
        audit=audit,
    )

    assert result.action == "create"
    assert result.issue_number == 100
    created = fake.get_issue("samhavens/scopewise", 100)
    assert created.title == "[manual] Backfill Scopewise ticket intake router"
    assert created.labels == ["source:manual", "owner:ops", "state:triage"]
    assert "- source: manual" in created.body
    assert "- severity: n/a" in created.body
    lines = _audit_lines(tmp_path / "audit.jsonl")
    assert len(lines) == 1
    assert lines[0]["action"] == "create"
    assert lines[0]["issue_number"] == 100


def test_update_deduped_issue_preserves_current_state(tmp_path: Path) -> None:
    fake = FakeGitHub()
    existing = GitHubIssue(
        number=42,
        url="https://github.com/samhavens/scopewise/issues/42",
        state="open",
        title="[alert][scopewise][prod] old",
        body="old body",
        labels=["source:alert", "owner:main", "state:in-progress", "bug"],
    )
    fake.seed(existing)
    bindings = BindingStore(tmp_path / "bindings.sqlite3")
    audit = AuditTrail(tmp_path / "audit.jsonl")
    payload = {
        "source": "alert",
        "owner": "ops",
        "summary": "Deploy safety regression",
        "routing_reason": "The issue is in Ductor-side deploy automation.",
        "next_action": "Keep investigating with ops-agent.",
        "repo": "scopewise",
        "environment": "prod",
        "fingerprint": "deploy-safety-123",
        "severity": "high",
        "evidence": {"count": 3},
    }
    event = _event(payload)
    bindings.bind(dedupe_key=event.dedupe_key or "", event=event, issue_number=42)

    result = route_event(
        event=event,
        raw_payload=payload,
        github=fake,
        bindings=bindings,
        audit=audit,
    )

    assert result.action == "update"
    assert result.issue_number == 42
    assert result.state == "in-progress"
    updated = fake.get_issue("samhavens/scopewise", 42)
    assert "owner:ops" in updated.labels
    assert "owner:main" not in updated.labels
    assert "state:in-progress" in updated.labels
    assert updated.title == "[alert][scopewise][prod] Deploy safety regression"
    assert "- state: in-progress" in updated.body
    assert bindings.get_issue_number("scopewise|prod|deploy-safety-123") == 42


def test_closed_bound_issue_creates_new_issue_and_rebinds(tmp_path: Path) -> None:
    fake = FakeGitHub()
    fake.seed(
        GitHubIssue(
            number=9,
            url="https://github.com/samhavens/scopewise/issues/9",
            state="closed",
            title="closed",
            body="closed",
            labels=["source:alert", "owner:ops", "state:done"],
        )
    )
    bindings = BindingStore(tmp_path / "bindings.sqlite3")
    audit = AuditTrail(tmp_path / "audit.jsonl")
    payload = {
        "source": "alert",
        "owner": "ops",
        "summary": "Recurring prod exception",
        "routing_reason": "Ops owns the intake lane.",
        "next_action": "Open a fresh triage issue.",
        "repo": "scopewise",
        "environment": "prod",
        "fingerprint": "exception-xyz",
    }
    event = _event(payload)
    bindings.bind(dedupe_key=event.dedupe_key or "", event=event, issue_number=9)

    result = route_event(
        event=event,
        raw_payload=payload,
        github=fake,
        bindings=bindings,
        audit=audit,
    )

    assert result.action == "create"
    assert result.issue_number == 100
    assert bindings.get_issue_number("scopewise|prod|exception-xyz") == 100


def test_dry_run_still_writes_audit_without_mutating_github(tmp_path: Path) -> None:
    fake = FakeGitHub()
    bindings = BindingStore(tmp_path / "bindings.sqlite3")
    audit = AuditTrail(tmp_path / "audit.jsonl")
    payload = {
        "source": "manual",
        "owner": "main",
        "summary": "Dry-run only",
        "routing_reason": "Validation path",
        "next_action": "Do not create a real issue.",
    }
    event = _event(payload)

    result = route_event(
        event=event,
        raw_payload=payload,
        github=fake,
        bindings=bindings,
        audit=audit,
        dry_run=True,
    )

    assert result.action == "dry-run:create"
    assert fake.issues == {}
    lines = _audit_lines(tmp_path / "audit.jsonl")
    assert lines[0]["dry_run"] is True
