"""Strict input types shared by the GitHub MCP tools.

Constraints live in the types so they appear in the tool's JSON Schema (the
model sees them) and are enforced before any handler code runs.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

Repo = Annotated[
    str,
    Field(
        pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$",
        description="Repository as owner/name, for example acme/demo-service",
    ),
]
Sha = Annotated[
    str,
    Field(pattern=r"^[0-9a-fA-F]{7,40}$", description="Git commit SHA (7-40 hex characters)"),
]
Ref = Annotated[
    str,
    Field(
        pattern=r"^[A-Za-z0-9._/-]{1,200}$",
        description="Branch name or commit SHA",
    ),
]
MAX_PATH_LEN = 300


def check_path(path: str) -> str:
    """Reject paths that could escape the repository or smuggle control characters."""
    bad = (
        not path
        or len(path) > MAX_PATH_LEN
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(part in ("..", ".") for part in path.split("/"))
    )
    if bad:
        raise ToolError(
            "invalid path: use a repository-relative path such as 'app/pricing.py' "
            "(no leading '/', no '..', no backslashes)"
        )
    return path


def clip(text: str, limit: int) -> tuple[str, bool]:
    """Truncate to ``limit`` characters; returns (text, was_truncated)."""
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...(truncated)", True
