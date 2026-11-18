"""Run the GitHub MCP server over stdio: ``python -m pd_mcp_github``."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pd_mcp_github.backend import MockBackend
from pd_mcp_github.server import build_server


def main() -> None:
    mode = os.environ.get("MCP_MODE", "mock")
    if mode != "mock":
        sys.exit(f"MCP_MODE={mode} is not supported yet; real GitHub arrives in Phase 10")
    backend = MockBackend.load(Path(os.environ.get("FIXTURES_DIR", "fixtures")))
    build_server(backend).run()  # stdio; stdout is the protocol, so log to stderr only


if __name__ == "__main__":
    main()
