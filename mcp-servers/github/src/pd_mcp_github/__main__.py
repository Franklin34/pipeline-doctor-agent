"""Run the GitHub MCP server over stdio: ``python -m pd_mcp_github``.

MCP_MODE=mock (default) serves fixtures. MCP_MODE=real talks to GitHub and needs:
  GITHUB_TOKEN          a fine-grained token limited to the target repositories
  GITHUB_ALLOWED_REPOS  comma-separated ``owner/name`` list; nothing else is reachable
  GITHUB_API_URL        optional, for GitHub Enterprise Server (must be https)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pd_mcp_github.backend import GitHubBackend, MockBackend
from pd_mcp_github.server import build_server


def build_backend(env: dict[str, str] | os._Environ[str]) -> GitHubBackend:
    mode = env.get("MCP_MODE", "mock")
    if mode == "mock":
        return MockBackend.load(Path(env.get("FIXTURES_DIR", "fixtures")))
    if mode == "real":
        from pd_mcp_github.rest import DEFAULT_API, RestBackend

        allowed = frozenset(
            r.strip() for r in env.get("GITHUB_ALLOWED_REPOS", "").split(",") if r.strip()
        )
        return RestBackend(
            env.get("GITHUB_TOKEN", ""), allowed, api_url=env.get("GITHUB_API_URL", DEFAULT_API)
        )
    raise ValueError(f"MCP_MODE must be mock or real, not '{mode}'")


def main() -> None:
    try:
        backend = build_backend(os.environ)
    except (ValueError, FileNotFoundError) as exc:
        sys.exit(f"github MCP server cannot start: {exc}")  # never prints the token
    build_server(backend).run()  # stdio; stdout is the protocol, so log to stderr only


if __name__ == "__main__":
    main()
