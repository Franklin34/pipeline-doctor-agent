"""Write restrictions, enforced in code. The model cannot argue its way past these."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

from mcp.server.mcpserver.exceptions import ToolError

BRANCH_PREFIX = "pipeline-doctor/"
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

# Files an automated change must never touch: pipeline and infrastructure definitions, build and
# test configuration (changing it can make tests pass without fixing anything), ownership rules,
# and anything that looks like a credential. Matched case-insensitively.
PROTECTED_DIRS = (
    ".github/",
    ".git/",
    ".circleci/",
    ".jenkins/",
    "deploy/",
    "infra/",
    "terraform/",
    "helm/",
)
PROTECTED_NAMES = (
    "jenkinsfile*",
    "dockerfile*",
    "docker-compose*",
    "codeowners",
    ".env*",
    "*.tf",
    "*.tfvars",
    "*.pem",
    "*.key",
    "*.p12",
    "id_rsa*",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "conftest.py",
    "makefile",
    ".pre-commit-config.yaml",
    "*.lock",
)
MAX_COMMIT_FILES = 10
MAX_FILE_BYTES = 100_000


def is_protected(path: str) -> bool:
    low = path.lower().lstrip("/")
    if any(low.startswith(d) or f"/{d}" in f"/{low}" for d in PROTECTED_DIRS):
        return True
    return any(fnmatchcase(posixpath.basename(low), pat) for pat in PROTECTED_NAMES)


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

    def check_commit(
        self, branch: str, default_branch: str, paths_and_sizes: list[tuple[str, int]], message: str
    ) -> None:
        """Rules for committing files: only to an agent branch, never to protected paths."""
        self._check_agent_branch(branch, default_branch, what="branch")
        if not message.strip():
            raise ToolError("commit message must not be blank")
        if not paths_and_sizes or len(paths_and_sizes) > MAX_COMMIT_FILES:
            raise ToolError(f"a commit must change between 1 and {MAX_COMMIT_FILES} files")
        seen: set[str] = set()
        for path, size in paths_and_sizes:
            if path in seen:
                raise ToolError(f"file '{path}' is listed twice")
            seen.add(path)
            if size > MAX_FILE_BYTES:
                raise ToolError(f"file '{path}' is larger than {MAX_FILE_BYTES} bytes")
            if is_protected(path):
                raise ToolError(
                    f"refused: '{path}' is a protected path (pipeline, infrastructure, build or test "
                    "configuration, ownership or credentials); automated changes may not touch it"
                )

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
