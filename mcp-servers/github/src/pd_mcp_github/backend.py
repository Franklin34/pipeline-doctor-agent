"""Backend contract plus the fixture-backed implementation used in mock mode."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel


class RawFile(BaseModel):
    path: str
    status: str
    patch: str


class RawCommit(BaseModel):
    sha: str
    author: str
    date: str
    message: str
    files: list[RawFile]


class RawFixtureFile(BaseModel):
    path: str
    ref: str
    content: str


class RawRepo(BaseModel):
    repo: str
    default_branch: str
    commits: list[RawCommit]  # oldest first
    files: list[RawFixtureFile] = []
    codeowners: str = ""


class GitHubBackend(Protocol):
    """What the tools need. A real REST/GraphQL backend arrives in Phase 10."""

    def default_branch(self, repo: str) -> str: ...
    def commits_between(self, repo: str, base: str, head: str) -> list[RawCommit]: ...
    def commit(self, repo: str, sha: str) -> RawCommit: ...
    def file(self, repo: str, path: str, ref: str) -> str: ...
    def codeowners(self, repo: str) -> str: ...
    def create_branch(self, repo: str, name: str, from_sha: str) -> str: ...
    def commit_files(self, repo: str, branch: str, message: str, files: dict[str, str]) -> str: ...
    def open_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> tuple[int, str]: ...


@dataclass
class MockBackend:
    """Serves ``fixtures/*/github.json``. Writes only touch in-memory state."""

    repos: dict[str, RawRepo]
    branches: dict[str, dict[str, str]] = field(default_factory=dict)  # repo -> name -> sha
    pull_requests: list[dict[str, object]] = field(default_factory=list)
    # repo -> branch -> path -> content (what commit_files wrote)
    branch_files: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    commits: list[dict[str, object]] = field(default_factory=list)

    @classmethod
    def load(cls, fixtures_dir: Path) -> MockBackend:
        paths = sorted(fixtures_dir.glob("*/github.json"))
        if not paths:
            raise FileNotFoundError(f"no github.json fixtures under {fixtures_dir}")
        repos: dict[str, RawRepo] = {}
        for p in paths:
            r = RawRepo.model_validate(json.loads(p.read_text()))
            if r.repo in repos:
                raise ValueError(f"{p}: duplicate repo {r.repo}")
            repos[r.repo] = r
        return cls(repos=repos)

    # -- helpers ---------------------------------------------------------
    def _repo(self, repo: str) -> RawRepo:
        try:
            return self.repos[repo]
        except KeyError:
            raise ToolError(
                f"repository '{repo}' not found or not accessible; "
                "use the repo from the PipelineFailed event"
            ) from None

    def _index(self, r: RawRepo, sha: str) -> int:
        matches = [i for i, c in enumerate(r.commits) if c.sha.startswith(sha.lower())]
        if len(matches) != 1:
            raise ToolError(
                f"commit '{sha}' not found in {r.repo} (or ambiguous); "
                "use SHAs from get_build or get_last_successful_build"
            )
        return matches[0]

    # -- reads -----------------------------------------------------------
    def default_branch(self, repo: str) -> str:
        return self._repo(repo).default_branch

    def commits_between(self, repo: str, base: str, head: str) -> list[RawCommit]:
        r = self._repo(repo)
        b, h = self._index(r, base), self._index(r, head)
        if b >= h:
            raise ToolError(
                "base_sha must be an ancestor of head_sha: pass the last green commit as base "
                "and the failing commit as head"
            )
        return r.commits[b + 1 : h + 1]

    def commit(self, repo: str, sha: str) -> RawCommit:
        r = self._repo(repo)
        return r.commits[self._index(r, sha)]

    def file(self, repo: str, path: str, ref: str) -> str:
        r = self._repo(repo)
        for f in r.files:
            if f.path == path and (f.ref == ref or f.ref.startswith(ref.lower())):
                return f.content
        raise ToolError(f"file '{path}' not found at ref '{ref}' in {repo}")

    def codeowners(self, repo: str) -> str:
        text = self._repo(repo).codeowners
        if not text:
            raise ToolError(f"{repo} has no CODEOWNERS file")
        return text

    # -- writes (in-memory only) ------------------------------------------
    def create_branch(self, repo: str, name: str, from_sha: str) -> str:
        r = self._repo(repo)
        full = r.commits[self._index(r, from_sha)].sha
        existing = self.branches.setdefault(repo, {})
        if name in existing:
            raise ToolError(f"branch '{name}' already exists in {repo}; choose a different name")
        existing[name] = full
        return full

    def commit_files(self, repo: str, branch: str, message: str, files: dict[str, str]) -> str:
        self._repo(repo)
        if branch not in self.branches.get(repo, {}):
            raise ToolError(f"branch '{branch}' does not exist; call create_branch first")
        self.branch_files.setdefault(repo, {}).setdefault(branch, {}).update(files)
        sha = hashlib.sha1(
            f"{repo}{branch}{message}{sorted(files.items())}".encode(), usedforsecurity=False
        ).hexdigest()
        self.commits.append(
            {"repo": repo, "branch": branch, "sha": sha, "message": message, "paths": sorted(files)}
        )
        return sha

    def open_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> tuple[int, str]:
        self._repo(repo)
        if head not in self.branches.get(repo, {}):
            raise ToolError(f"head branch '{head}' does not exist; call create_branch first")
        if not self.branch_files.get(repo, {}).get(head):
            raise ToolError(
                f"branch '{head}' has no changes; call commit_files first (an empty pull request is not allowed)"
            )
        for pr in self.pull_requests:  # idempotent: one open PR per head branch
            if pr["repo"] == repo and pr["head"] == head and pr["base"] == base:
                return int(str(pr["number"])), f"https://github.example/{repo}/pull/{pr['number']}"
        number = len(self.pull_requests) + 1
        self.pull_requests.append(
            {
                "repo": repo,
                "number": number,
                "head": head,
                "base": base,
                "title": title,
                "body": body,
                "draft": True,
            }
        )
        return number, f"https://github.example/{repo}/pull/{number}"
