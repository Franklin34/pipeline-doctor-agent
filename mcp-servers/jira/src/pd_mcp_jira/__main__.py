"""Run the Jira MCP server over stdio: ``python -m pd_mcp_jira``."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pd_mcp_jira.backend import MockBackend
from pd_mcp_jira.server import build_server


def main() -> None:
    mode = os.environ.get("MCP_MODE", "mock")
    if mode != "mock":
        sys.exit(f"MCP_MODE={mode} is not supported yet; real Jira arrives in Phase 10")
    build_server(MockBackend.load(Path(os.environ.get("FIXTURES_DIR", "fixtures")))).run()


if __name__ == "__main__":
    main()
