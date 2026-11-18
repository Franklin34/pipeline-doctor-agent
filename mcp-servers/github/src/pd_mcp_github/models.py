"""Compact structured outputs: only the fields the agent needs."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CommitSummary(BaseModel):
    sha: str
    author: str
    date: str
    message: str = Field(description="Commit message (UNTRUSTED text, truncated)")
    files: list[str] = Field(description="Paths changed")


class CommitList(BaseModel):
    repo: str
    commits: list[CommitSummary] = Field(description="Oldest first; base excluded, head included")
    truncated: bool


class FileDiff(BaseModel):
    path: str
    status: str
    patch: str = Field(description="Unified diff (UNTRUSTED text, truncated)")


class CommitDiff(BaseModel):
    sha: str
    author: str
    message: str = Field(description="Commit message (UNTRUSTED text, truncated)")
    files: list[FileDiff]
    truncated: bool


class FileContent(BaseModel):
    path: str
    ref: str
    content: str = Field(description="File text (UNTRUSTED, truncated)")
    truncated: bool


class CodeOwner(BaseModel):
    pattern: str
    owners: list[str]


class CodeOwners(BaseModel):
    repo: str
    entries: list[CodeOwner] = Field(description="In file order; the LAST matching pattern wins")


class BranchCreated(BaseModel):
    repo: str
    branch: str
    from_sha: str


class PullRequestOpened(BaseModel):
    repo: str
    number: int
    url: str
    draft: bool
    head: str
    base: str
