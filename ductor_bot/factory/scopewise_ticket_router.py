"""Deterministic Scopewise Software Factory ticket intake.

This module implements the first narrow Ductor-side intake slice:

- accept a normalized event JSON payload
- create or update a GitHub issue in ``samhavens/scopewise``
- enforce one source/owner/state label each
- keep a local dedupe binding store
- append a local audit trail for every attempted route

The transport layer is intentionally out of scope here. Webhooks or task
automation can call this module later without embedding policy in the trigger.
"""

# ruff: noqa: UP017

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

DEFAULT_GITHUB_REPO = "samhavens/scopewise"
DEFAULT_SCOPEWISE_REPO = "scopewise"
DEFAULT_STATE = "triage"

SOURCE_LABELS = {
    "alert": "source:alert",
    "agent": "source:agent",
    "manual": "source:manual",
}
OWNER_LABELS = {
    "main": "owner:main",
    "ops": "owner:ops",
    "rl": "owner:rl",
}
STATE_LABELS = {
    "triage": "state:triage",
    "ready": "state:ready",
    "in-progress": "state:in-progress",
    "blocked": "state:blocked",
    "pr-open": "state:pr-open",
    "done": "state:done",
}

SOURCE_PREFIX = "source:"
OWNER_PREFIX = "owner:"
STATE_PREFIX = "state:"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_text(value: Any, *, default: str = "n/a") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _normalize_summary(value: Any) -> str:
    text = _normalize_text(value, default="")
    text = " ".join(text.split())
    if not text:
        msg = "summary is required"
        raise ValueError(msg)
    return text


def _normalize_choice(value: Any, choices: dict[str, str], *, field_name: str) -> str:
    text = _normalize_text(value, default="").lower()
    if text not in choices:
        msg = f"{field_name} must be one of: {', '.join(sorted(choices))}"
        raise ValueError(msg)
    return text


def _normalize_count(value: Any) -> str:
    if value in (None, "", "n/a"):
        return "n/a"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


@dataclass(frozen=True)
class Evidence:
    """Human-readable evidence block for the issue body."""

    links: list[str] = field(default_factory=list)
    first_seen: str = "n/a"
    last_seen: str = "n/a"
    count: str = "n/a"

    @classmethod
    def from_payload(cls, payload: Any) -> Evidence:
        if payload is None:
            return cls()
        if not isinstance(payload, dict):
            msg = "evidence must be an object when provided"
            raise TypeError(msg)
        raw_links = payload.get("links", [])
        if raw_links in (None, "", "n/a"):
            links: list[str] = []
        elif isinstance(raw_links, list):
            links = [_normalize_text(item) for item in raw_links if _normalize_text(item) != "n/a"]
        else:
            links = [_normalize_text(raw_links)]
            if links == ["n/a"]:
                links = []
        return cls(
            links=links,
            first_seen=_normalize_text(payload.get("first_seen")),
            last_seen=_normalize_text(payload.get("last_seen")),
            count=_normalize_count(payload.get("count")),
        )


@dataclass(frozen=True)
class DedupeRefs:
    """Optional GitHub linkage fields mirrored into the issue body."""

    related_issue: str = "n/a"
    related_pr: str = "n/a"

    @classmethod
    def from_payload(cls, payload: Any) -> DedupeRefs:
        if payload is None:
            return cls()
        if not isinstance(payload, dict):
            msg = "dedupe must be an object when provided"
            raise TypeError(msg)
        return cls(
            related_issue=_normalize_text(payload.get("related_issue")),
            related_pr=_normalize_text(payload.get("related_pr")),
        )


@dataclass(frozen=True)
class NormalizedTicketEvent:
    """Validated issue-intake input."""

    source: str
    owner: str
    summary: str
    routing_reason: str
    next_action: str
    repo: str = DEFAULT_SCOPEWISE_REPO
    github_repo: str = DEFAULT_GITHUB_REPO
    environment: str = "n/a"
    fingerprint: str = "n/a"
    severity: str = "n/a"
    state: str = DEFAULT_STATE
    issue_number: int | None = None
    trigger: str = "manual-cli"
    evidence: Evidence = field(default_factory=Evidence)
    dedupe: DedupeRefs = field(default_factory=DedupeRefs)
    preserve_state_on_update: bool = True
    force_state: bool = False

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any], *, github_repo_default: str
    ) -> NormalizedTicketEvent:
        if not isinstance(payload, dict):
            msg = "event payload must be a JSON object"
            raise TypeError(msg)

        issue_number_raw = payload.get("issue_number")
        issue_number = None if issue_number_raw in (None, "", "n/a") else int(issue_number_raw)
        preserve_state_on_update = bool(payload.get("preserve_state_on_update", True))
        force_state = bool(payload.get("force_state", False))

        return cls(
            source=_normalize_choice(payload.get("source"), SOURCE_LABELS, field_name="source"),
            owner=_normalize_choice(payload.get("owner"), OWNER_LABELS, field_name="owner"),
            summary=_normalize_summary(payload.get("summary")),
            routing_reason=_normalize_text(payload.get("routing_reason")),
            next_action=_normalize_text(payload.get("next_action")),
            repo=_normalize_text(payload.get("repo"), default=DEFAULT_SCOPEWISE_REPO),
            github_repo=_normalize_text(payload.get("github_repo"), default=github_repo_default),
            environment=_normalize_text(payload.get("environment")).lower(),
            fingerprint=_normalize_text(payload.get("fingerprint")),
            severity=_normalize_text(payload.get("severity")).lower(),
            state=_normalize_choice(
                payload.get("state", DEFAULT_STATE),
                STATE_LABELS,
                field_name="state",
            ),
            issue_number=issue_number,
            trigger=_normalize_text(payload.get("trigger"), default="manual-cli"),
            evidence=Evidence.from_payload(payload.get("evidence")),
            dedupe=DedupeRefs.from_payload(payload.get("dedupe")),
            preserve_state_on_update=preserve_state_on_update,
            force_state=force_state,
        )

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS[self.source]

    @property
    def owner_label(self) -> str:
        return OWNER_LABELS[self.owner]

    def state_label_for(self, state: str) -> str:
        return STATE_LABELS[state]

    @property
    def dedupe_key(self) -> str | None:
        if self.fingerprint == "n/a" or self.environment == "n/a":
            return None
        return f"{self.repo}|{self.environment}|{self.fingerprint}"


@dataclass(frozen=True)
class GitHubIssue:
    """Subset of issue state needed for routing."""

    number: int
    url: str
    state: str
    title: str
    body: str
    labels: list[str]

    @property
    def state_label(self) -> str | None:
        for label in self.labels:
            if label.startswith(STATE_PREFIX):
                return label
        return None


@dataclass(frozen=True)
class RouteResult:
    """Structured result emitted to stdout and written into the audit log."""

    action: str
    issue_number: int | None
    issue_url: str | None
    github_repo: str
    title: str
    state: str
    labels: list[str]
    dedupe_key: str | None
    dry_run: bool
    audit_log: str
    binding_db: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "issue_number": self.issue_number,
            "issue_url": self.issue_url,
            "github_repo": self.github_repo,
            "title": self.title,
            "state": self.state,
            "labels": self.labels,
            "dedupe_key": self.dedupe_key,
            "dry_run": self.dry_run,
            "audit_log": self.audit_log,
            "binding_db": self.binding_db,
        }


class GitHubClient(Protocol):
    """Abstraction so routing logic can be unit-tested without shelling out."""

    def get_issue(self, repo: str, issue_number: int) -> GitHubIssue:
        """Return a single GitHub issue."""

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> GitHubIssue:
        """Create and return a GitHub issue."""

    def update_issue(  # noqa: PLR0913
        self,
        repo: str,
        issue_number: int,
        *,
        title: str,
        body: str,
        add_labels: list[str],
        remove_labels: list[str],
    ) -> GitHubIssue:
        """Update an issue and return the refreshed issue."""


class GitHubCLIError(RuntimeError):
    """Raised when the gh CLI fails."""


class GitHubCLI:
    """Minimal gh-backed implementation for issue create/update/view."""

    def __init__(self, *, gh_bin: str = "gh") -> None:
        self._gh_bin = gh_bin

    def _run(self, args: list[str], *, body: str | None = None) -> str:
        cmd = [self._gh_bin, *args]
        try:
            completed = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                input=body,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout or str(exc)
            msg = f"gh command failed: {' '.join(cmd)} :: {detail}"
            raise GitHubCLIError(msg) from exc
        return completed.stdout.strip()

    def get_issue(self, repo: str, issue_number: int) -> GitHubIssue:
        stdout = self._run(
            [
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo,
                "--json",
                "number,url,state,title,body,labels",
            ]
        )
        payload = json.loads(stdout)
        labels = [entry["name"] for entry in payload.get("labels", []) if "name" in entry]
        return GitHubIssue(
            number=int(payload["number"]),
            url=str(payload["url"]),
            state=str(payload["state"]).lower(),
            title=str(payload["title"]),
            body=str(payload.get("body", "")),
            labels=labels,
        )

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> GitHubIssue:
        args = [
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body-file",
            "-",
        ]
        for label in labels:
            args.extend(["--label", label])
        stdout = self._run(args, body=body)
        match = re.search(r"/issues/(?P<number>\d+)$", stdout.strip())
        if not match:
            msg = f"could not parse issue number from gh output: {stdout!r}"
            raise GitHubCLIError(msg)
        issue_number = int(match.group("number"))
        return self.get_issue(repo, issue_number)

    def update_issue(  # noqa: PLR0913
        self,
        repo: str,
        issue_number: int,
        *,
        title: str,
        body: str,
        add_labels: list[str],
        remove_labels: list[str],
    ) -> GitHubIssue:
        args = [
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            repo,
            "--title",
            title,
            "--body-file",
            "-",
        ]
        for label in add_labels:
            args.extend(["--add-label", label])
        for label in remove_labels:
            args.extend(["--remove-label", label])
        self._run(args, body=body)
        return self.get_issue(repo, issue_number)


class BindingStore:
    """SQLite-backed dedupe bindings for alert/agent events."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dedupe_bindings (
                    dedupe_key TEXT PRIMARY KEY,
                    github_repo TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get_issue_number(self, dedupe_key: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT issue_number FROM dedupe_bindings WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
        if row is None:
            return None
        return int(row[0])

    def bind(self, *, dedupe_key: str, event: NormalizedTicketEvent, issue_number: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dedupe_bindings (
                    dedupe_key, github_repo, repo, environment, fingerprint, issue_number, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    github_repo = excluded.github_repo,
                    repo = excluded.repo,
                    environment = excluded.environment,
                    fingerprint = excluded.fingerprint,
                    issue_number = excluded.issue_number,
                    updated_at = excluded.updated_at
                """,
                (
                    dedupe_key,
                    event.github_repo,
                    event.repo,
                    event.environment,
                    event.fingerprint,
                    issue_number,
                    _utc_now_iso(),
                ),
            )


class AuditTrail:
    """Append-only JSONL audit trail."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _format_links(links: list[str]) -> list[str]:
    if not links:
        return ["  - n/a"]
    return [f"  - {link}" for link in links]


def render_issue_title(event: NormalizedTicketEvent) -> str:
    if event.source == "alert":
        return f"[alert][{event.repo}][{event.environment}] {event.summary}"
    if event.source == "agent":
        return f"[agent][{event.owner}] {event.summary}"
    return f"[manual] {event.summary}"


def render_issue_body(event: NormalizedTicketEvent, *, state: str) -> str:
    lines = [
        "## Source",
        f"- source: {event.source}",
        f"- owner: {event.owner}",
        f"- state: {state}",
        "",
        "## Summary",
        event.summary,
        "",
        "## Context",
        f"- repo: {event.repo}",
        f"- environment: {event.environment}",
        f"- fingerprint: {event.fingerprint}",
        f"- severity: {event.severity}",
        "",
        "## Evidence",
        "- links:",
        *_format_links(event.evidence.links),
        f"- first seen: {event.evidence.first_seen}",
        f"- last seen: {event.evidence.last_seen}",
        f"- count: {event.evidence.count}",
        "",
        "## Routing Reason",
        event.routing_reason,
        "",
        "## Next Action",
        event.next_action,
        "",
        "## Dedupe",
        f"- related issue: {event.dedupe.related_issue}",
        f"- related PR: {event.dedupe.related_pr}",
    ]
    return "\n".join(lines).strip() + "\n"


def _replace_group(
    labels: list[str], desired_label: str, *, prefix: str
) -> tuple[list[str], list[str]]:
    current_group = [label for label in labels if label.startswith(prefix)]
    remove = [label for label in current_group if label != desired_label]
    add = [] if desired_label in labels else [desired_label]
    return add, remove


def _merge_labels(
    current_labels: list[str], *, source_label: str, owner_label: str, state_label: str
) -> tuple[list[str], list[str], list[str]]:
    add_source, remove_source = _replace_group(current_labels, source_label, prefix=SOURCE_PREFIX)
    add_owner, remove_owner = _replace_group(current_labels, owner_label, prefix=OWNER_PREFIX)
    add_state, remove_state = _replace_group(current_labels, state_label, prefix=STATE_PREFIX)
    final = [
        label
        for label in current_labels
        if label not in {*remove_source, *remove_owner, *remove_state}
    ]
    for label in [*add_source, *add_owner, *add_state]:
        if label not in final:
            final.append(label)
    return (
        final,
        [*add_source, *add_owner, *add_state],
        [*remove_source, *remove_owner, *remove_state],
    )


def _state_name_from_label(label: str | None) -> str | None:
    if not label:
        return None
    if not label.startswith(STATE_PREFIX):
        return None
    return label.split(":", 1)[1]


def route_event(  # noqa: C901,PLR0912,PLR0913,PLR0915
    *,
    event: NormalizedTicketEvent,
    raw_payload: dict[str, Any],
    github: GitHubClient,
    bindings: BindingStore,
    audit: AuditTrail,
    dry_run: bool = False,
) -> RouteResult:
    target_issue: GitHubIssue | None = None
    dedupe_key = event.dedupe_key

    if event.issue_number is not None:
        target_issue = github.get_issue(event.github_repo, event.issue_number)
    elif dedupe_key is not None:
        bound_issue_number = bindings.get_issue_number(dedupe_key)
        if bound_issue_number is not None:
            bound_issue = github.get_issue(event.github_repo, bound_issue_number)
            if bound_issue.state == "open":
                target_issue = bound_issue

    if target_issue is None:
        if event.state != DEFAULT_STATE:
            msg = "new intake must start in state:triage"
            raise ValueError(msg)
        resolved_state = DEFAULT_STATE
        title = render_issue_title(event)
        body = render_issue_body(event, state=resolved_state)
        labels = [event.source_label, event.owner_label, event.state_label_for(resolved_state)]
        if dry_run:
            routed_issue_number = event.issue_number
            routed_issue_url = None
            action = "dry-run:create"
        else:
            created = github.create_issue(event.github_repo, title, body, labels)
            routed_issue_number = created.number
            routed_issue_url = created.url
            action = "create"
            if dedupe_key is not None:
                bindings.bind(dedupe_key=dedupe_key, event=event, issue_number=created.number)
    else:
        existing_state = _state_name_from_label(target_issue.state_label)
        if event.force_state:
            resolved_state = event.state
        elif event.preserve_state_on_update and existing_state is not None:
            resolved_state = existing_state
        else:
            resolved_state = event.state
        title = render_issue_title(event)
        body = render_issue_body(event, state=resolved_state)
        labels, add_labels, remove_labels = _merge_labels(
            target_issue.labels,
            source_label=event.source_label,
            owner_label=event.owner_label,
            state_label=event.state_label_for(resolved_state),
        )
        if dry_run:
            routed_issue_number = target_issue.number
            routed_issue_url = target_issue.url
            action = "dry-run:update"
        else:
            updated = github.update_issue(
                event.github_repo,
                target_issue.number,
                title=title,
                body=body,
                add_labels=add_labels,
                remove_labels=remove_labels,
            )
            routed_issue_number = updated.number
            routed_issue_url = updated.url
            action = "update"
            if dedupe_key is not None:
                bindings.bind(dedupe_key=dedupe_key, event=event, issue_number=updated.number)

    audit.append(
        {
            "timestamp": _utc_now_iso(),
            "action": action,
            "issue_number": routed_issue_number,
            "issue_url": routed_issue_url,
            "github_repo": event.github_repo,
            "repo": event.repo,
            "source": event.source,
            "owner": event.owner,
            "state": resolved_state,
            "labels": labels,
            "dedupe_key": dedupe_key,
            "trigger": event.trigger,
            "dry_run": dry_run,
            "payload": raw_payload,
        }
    )

    return RouteResult(
        action=action,
        issue_number=routed_issue_number,
        issue_url=routed_issue_url,
        github_repo=event.github_repo,
        title=title,
        state=resolved_state,
        labels=labels,
        dedupe_key=dedupe_key,
        dry_run=dry_run,
        audit_log=str(audit.log_path),
        binding_db=str(bindings.db_path),
    )


def _default_state_dir() -> Path:
    ductor_home = Path(os.environ.get("DUCTOR_HOME", str(Path.home() / ".ductor"))).expanduser()
    return ductor_home / "workspace" / "software_factory" / "scopewise_ticket_router"


def _load_payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.event_json:
        return json.loads(args.event_json)
    if args.event_file:
        return json.loads(Path(args.event_file).read_text(encoding="utf-8"))
    if sys.stdin.isatty():
        msg = "provide --event-json, --event-file, or JSON on stdin"
        raise ValueError(msg)
    return json.loads(sys.stdin.read())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or update Scopewise Software Factory issues from normalized events."
    )
    parser.add_argument(
        "--event-file",
        help="Path to a normalized event JSON file. If omitted, reads stdin unless --event-json is set.",
    )
    parser.add_argument("--event-json", help="Normalized event JSON string.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Render and audit without mutating GitHub."
    )
    parser.add_argument(
        "--state-dir",
        default=str(_default_state_dir()),
        help="Directory for the local binding DB and JSONL audit log.",
    )
    parser.add_argument(
        "--github-repo",
        default=DEFAULT_GITHUB_REPO,
        help="Target GitHub repo in OWNER/REPO form. Defaults to samhavens/scopewise.",
    )
    parser.add_argument(
        "--gh-bin",
        default=os.environ.get("GH_BIN", "gh"),
        help="gh CLI binary to use.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    raw_payload = _load_payload_from_args(args)
    event = NormalizedTicketEvent.from_payload(raw_payload, github_repo_default=args.github_repo)

    state_dir = Path(args.state_dir).expanduser().resolve()
    bindings = BindingStore(state_dir / "dedupe_bindings.sqlite3")
    audit = AuditTrail(state_dir / "audit" / "scopewise_ticket_router.jsonl")
    github = GitHubCLI(gh_bin=args.gh_bin)

    result = route_event(
        event=event,
        raw_payload=raw_payload,
        github=github,
        bindings=bindings,
        audit=audit,
        dry_run=args.dry_run,
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
