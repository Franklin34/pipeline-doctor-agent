"""Write restrictions, enforced in code. The model cannot argue its way past these."""

from __future__ import annotations

import re
from dataclasses import dataclass

from mcp.server.mcpserver.exceptions import ToolError

BRANCH_PREFIX = "pipeline-doctor/"
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


@dataclass(frozen=True)
class WritePolicy:
    """Rules for the two write tools.

    * Branches may only be created under ``branch_prefix``.
    * Pull requests may only come from such a branch, and only target the default
      branch (they are always drafts, so a human must still review and merge).
    * Nothing here ever pushes to, or modifies, the default branch.
    """

    branch_prefix: str = BRANCH_PREFIX

    def check_new_branch(self, name: str, default_branch: str) -> None:
        self._check_agent_branch(name, default_branch, what="branch")

    def check_pull_request(self, head: str, base: str, default_branch: str) -> None:
        self._check_agent_branch(head, default_branch, what="head branch")
        if base != default_branch:
            raise ToolError(
                f"refused: pull requests may only target the default branch '{default_branch}', "
                f"not '{base}'"
            )

    def _check_agent_branch(self, name: str, default_branch: str, *, what: str) -> None:
        if name == default_branch:
            raise ToolError(
                f"refused: writing to the default branch '{default_branch}' is not allowed"
            )
        if not name.startswith(self.branch_prefix):
            raise ToolError(
                f"refused: {what} must start with '{self.branch_prefix}' (got '{name}'); "
                f"example: '{self.branch_prefix}fix-pricing-12'"
            )
        rest = name[len(self.branch_prefix) :]
        if (
            not _SAFE_BRANCH.match(name)
            or not rest
            or ".." in name
            or "//" in name
            or name.endswith(("/", ".lock", "."))
        ):
            raise ToolError(f"invalid {what} name '{name}'")
