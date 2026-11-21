"""Backend contract plus the fixture-backed mock used in mock mode."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel


class Issue(BaseModel):
    key: str
    summary: str
    status: str
    fingerprint: str
    url: str
    description: str = ""
    category: str = ""
    assignee: str | None = None
    labels: list[str] = []
    comments: list[str] = []


class JiraBackend(Protocol):
    """One Jira project only: the project is configuration, never a tool argument."""

    def search_by_fingerprint(self, fingerprint: str) -> list[Issue]: ...
    def create_issue(self, issue: Issue) -> Issue: ...
    def add_comment(self, key: str, body: str, marker: str | None = None) -> bool:
        """Add a comment; with a marker, do nothing and return False if it is already present."""
        ...


CLOSED = {"done", "closed", "resolved"}


@dataclass
class MockBackend:
    """Seeded from ``fixtures/*/jira.json``. State is in memory and never persisted."""

    project: str
    issues: dict[str, Issue]
    _counter: int = field(default=100)

    @classmethod
    def load(cls, fixtures_dir: Path) -> MockBackend:
        paths = sorted(fixtures_dir.glob("*/jira.json"))
        if not paths:
            raise FileNotFoundError(f"no jira.json fixtures under {fixtures_dir}")
        project, issues = "", {}
        for p in paths:
            raw = json.loads(p.read_text())
            project = project or raw["project"]
            if raw["project"] != project:
                raise ValueError(f"{p}: all fixtures must use one project ({project})")
            for i in raw["issues"]:
                issue = Issue.model_validate(i)
                issues[issue.key] = issue
        return cls(project=project, issues=issues)

    def search_by_fingerprint(self, fingerprint: str) -> list[Issue]:
        return [i for i in self.issues.values() if i.fingerprint == fingerprint]

    def create_issue(self, issue: Issue) -> Issue:
        self._counter += 1
        key = f"{self.project}-{self._counter}"
        created = issue.model_copy(
            update={
                "key": key,
                "status": "Open",
                "url": f"https://example.atlassian.net/browse/{key}",
            }
        )
        self.issues[key] = created
        return created

    def add_comment(self, key: str, body: str, marker: str | None = None) -> bool:
        if key not in self.issues:
            raise ToolError(
                f"issue '{key}' not found in project {self.project}; search by fingerprint first"
            )
        issue = self.issues[key]
        if marker and any(marker in c for c in issue.comments):
            return False
        issue.comments.append(body)
        return True
