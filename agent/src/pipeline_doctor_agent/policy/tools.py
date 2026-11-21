"""Tool-call policy: every call passes through here before it executes.

Three controls, all enforced in code:
1. Per-agent allowlist: an agent can only call the tools it needs.
2. Scope pinning: ``job`` and ``repo`` arguments must match the run's event, so injected text
   cannot steer an agent at another job or repository.
3. Argument sanity: oversized or non-scalar values are refused.

Write tools appear in no allowlist until Phase 7 grants them to the fix agent alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

JENKINS_READ = frozenset(
    {"get_build", "get_failed_log_excerpt", "get_test_report", "get_last_successful_build"}
)
GITHUB_READ = frozenset({"list_commits_between", "get_commit_diff", "get_file", "get_codeowners"})
WRITE_TOOLS = frozenset(
    {
        "create_branch",
        "commit_files",
        "open_draft_pull_request",
        "create_issue",
        "add_comment",
        "rebuild",
    }
)

DEFAULT_ALLOWLIST: Mapping[str, frozenset[str]] = {
    "log_analyst": JENKINS_READ,
    "code_investigator": GITHUB_READ,
    "knowledge_agent": frozenset({"search_knowledge"}),
    "diagnose_agent": frozenset(),  # reasons over findings; no tools at all
    # The LLM that proposes a code change. It gets NO tools: code builds its context and applies,
    # tests and publishes whatever it proposes.
    "fix_agent": frozenset(),
    # Plain code: the only identity holding the git write tools. Every one is also restricted by the
    # GitHub MCP server itself (branch prefix, protected paths, draft-only, default branch only).
    "pr_publisher": frozenset(
        {"get_repo_info", "create_branch", "commit_files", "open_draft_pull_request"}
    ),
    # Plain code, not an LLM: renders the ticket and writes it. The only holder of Jira write tools.
    "ticket_agent": frozenset(
        {
            "search_similar_issues",
            "create_issue",
            "add_comment",
            "get_commit_diff",
            "get_codeowners",
        }
    ),
}
MAX_ARG_CHARS = 1_000  # default per string argument: investigators only send short identifiers
# Agents that legitimately write long text (a ticket body); the MCP servers enforce their own caps too.
ARG_LIMITS: Mapping[str, int] = {"ticket_agent": 20_000, "pr_publisher": 1_000_000}
# List-valued arguments an agent may send (file contents for a commit), bounded by serialised size.
STRUCTURED_ARGS: Mapping[str, frozenset[str]] = {"pr_publisher": frozenset({"files"})}


@dataclass(frozen=True)
class RunScope:
    job_name: str
    repo: str | None


@dataclass(frozen=True)
class ToolPolicy:
    scope: RunScope
    allowlist: Mapping[str, frozenset[str]] = field(default_factory=lambda: dict(DEFAULT_ALLOWLIST))

    def check(self, agent: str, tool: str, args: Mapping[str, Any]) -> str | None:
        """Return a refusal reason, or None if the call may proceed."""
        allowed = self.allowlist.get(agent)
        if allowed is None:
            return f"unknown agent '{agent}' has no tool permissions"
        if tool not in allowed:
            return f"tool '{tool}' is not permitted for agent '{agent}'"
        for name, value in args.items():
            if isinstance(value, str) and len(value) > ARG_LIMITS.get(agent, MAX_ARG_CHARS):
                return f"argument '{name}' is too long"
            if isinstance(value, list) and name in STRUCTURED_ARGS.get(agent, frozenset()):
                if len(json.dumps(value)) > ARG_LIMITS.get(agent, MAX_ARG_CHARS):
                    return f"argument '{name}' is too large"
                continue
            if not isinstance(value, str | int | float | bool | None):
                return f"argument '{name}' must be a plain value"
        if "job" in args and args["job"] != self.scope.job_name:
            return f"job must be '{self.scope.job_name}' (the failing job), not '{args['job']}'"
        if "repo" in args:
            if self.scope.repo is None:
                return "no repository is configured for this run"
            if args["repo"] != self.scope.repo:
                return f"repo must be '{self.scope.repo}' (the failing repository), not '{args['repo']}'"
        return None
