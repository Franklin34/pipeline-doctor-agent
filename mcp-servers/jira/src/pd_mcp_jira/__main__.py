"""Run the Jira MCP server over stdio: ``python -m pd_mcp_jira``.

MCP_MODE=mock (default) serves fixtures. MCP_MODE=real talks to Jira Cloud and needs:
  JIRA_BASE_URL      https://<site>.atlassian.net
  JIRA_EMAIL         the service account's email
  JIRA_API_TOKEN     its API token (access to ONE project only)
  JIRA_PROJECT_KEY   the only project this server can touch
  JIRA_ISSUE_TYPE    optional, default Task
  JIRA_USER_MAP_FILE optional JSON {"handle": "accountId"} used to assign code owners
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pd_mcp_jira.backend import JiraBackend, MockBackend
from pd_mcp_jira.server import build_server


def build_backend(env: dict[str, str] | os._Environ[str]) -> JiraBackend:
    mode = env.get("MCP_MODE", "mock")
    if mode == "mock":
        return MockBackend.load(Path(env.get("FIXTURES_DIR", "fixtures")))
    if mode == "real":
        from pd_mcp_jira.rest import JiraRestBackend, load_user_map

        return JiraRestBackend(
            env.get("JIRA_BASE_URL", ""),
            env.get("JIRA_EMAIL", ""),
            env.get("JIRA_API_TOKEN", ""),
            env.get("JIRA_PROJECT_KEY", ""),
            issue_type=env.get("JIRA_ISSUE_TYPE", "Task"),
            user_map=load_user_map(env.get("JIRA_USER_MAP_FILE") or None),
        )
    raise ValueError(f"MCP_MODE must be mock or real, not '{mode}'")


def main() -> None:
    try:
        backend = build_backend(os.environ)
    except (ValueError, OSError) as exc:
        sys.exit(f"jira MCP server cannot start: {exc}")  # never prints the token
    build_server(backend).run()


if __name__ == "__main__":
    main()
