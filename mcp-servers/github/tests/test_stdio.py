"""Launch the server as a real subprocess: stdout must carry only protocol messages."""

from __future__ import annotations

import sys
from pathlib import Path

from mcp import StdioServerParameters
from mcp.client import Client

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


async def test_stdio_roundtrip() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pd_mcp_github"],
        env={"MCP_MODE": "mock", "FIXTURES_DIR": str(FIXTURES)},
    )
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "list_commits_between" in names
