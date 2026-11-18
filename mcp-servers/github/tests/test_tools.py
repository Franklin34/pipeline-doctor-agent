from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp.client import Client
from pd_mcp_github.backend import MockBackend
from pd_mcp_github.server import build_server

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
REPO = "acme/demo-service"
A = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
B = "b2c3d4e5f60718293a4b5c6d7e8f901234567890"
C = "c3d4e5f60718293a4b5c6d7e8f90123456789012"


@pytest.fixture
def backend() -> MockBackend:
    return MockBackend.load(FIXTURES)


async def call(backend: MockBackend, tool: str, args: dict[str, Any]) -> tuple[bool, str, Any]:
    async with Client(build_server(backend)) as client:
        res = await client.call_tool(tool, args)
    text = "".join(getattr(c, "text", "") for c in res.content)
    return bool(res.is_error), text, res.structured_content


async def test_lists_expected_tools(backend: MockBackend) -> None:
    async with Client(build_server(backend)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "list_commits_between",
        "get_commit_diff",
        "get_file",
        "get_codeowners",
        "create_branch",
        "open_draft_pull_request",
    }
    assert (
        tools["get_file"].annotations is not None and tools["get_file"].annotations.read_only_hint
    )
    # Untrusted-content warning travels with the tool description.
    assert tools["get_commit_diff"].description is not None
    assert "UNTRUSTED" in tools["get_commit_diff"].description


async def test_list_commits_between_excludes_base_includes_head(backend: MockBackend) -> None:
    err, _, data = await call(
        backend, "list_commits_between", {"repo": REPO, "base_sha": A, "head_sha": C}
    )
    assert not err
    assert [c["sha"] for c in data["commits"]] == [B, C]
    assert data["commits"][1]["files"] == ["app/pricing.py"]


async def test_short_sha_accepted(backend: MockBackend) -> None:
    err, _, data = await call(
        backend, "list_commits_between", {"repo": REPO, "base_sha": A[:7], "head_sha": C[:7]}
    )
    assert not err and len(data["commits"]) == 2


@pytest.mark.parametrize(
    ("tool", "args", "needle"),
    [
        ("list_commits_between", {"repo": REPO, "base_sha": C, "head_sha": A}, "ancestor"),
        ("list_commits_between", {"repo": REPO, "base_sha": "deadbee", "head_sha": C}, "not found"),
        (
            "list_commits_between",
            {"repo": "acme/unknown", "base_sha": A, "head_sha": C},
            "not found",
        ),
        ("list_commits_between", {"repo": "no-slash", "base_sha": A, "head_sha": C}, "pattern"),
        ("list_commits_between", {"repo": REPO, "base_sha": "zzzzzzz", "head_sha": C}, "pattern"),
        ("get_commit_diff", {"repo": REPO, "sha": "not-a-sha"}, "pattern"),
        ("get_file", {"repo": REPO, "path": "../etc/passwd", "ref": "main"}, "invalid path"),
        ("get_file", {"repo": REPO, "path": "/etc/passwd", "ref": "main"}, "invalid path"),
        ("get_file", {"repo": REPO, "path": "a\\b", "ref": "main"}, "invalid path"),
        ("get_file", {"repo": REPO, "path": "missing.py", "ref": "main"}, "not found"),
        ("get_file", {"repo": REPO, "path": "app/pricing.py", "ref": "main; rm -rf"}, "pattern"),
    ],
)
async def test_read_tool_rejections(
    backend: MockBackend, tool: str, args: dict[str, Any], needle: str
) -> None:
    err, text, _ = await call(backend, tool, args)
    assert err, text
    assert needle in text


async def test_commit_diff_and_file(backend: MockBackend) -> None:
    err, _, diff = await call(backend, "get_commit_diff", {"repo": REPO, "sha": C})
    assert not err
    assert diff["files"][0]["path"] == "app/pricing.py"
    assert "return price * rate" in diff["files"][0]["patch"]

    err, _, f = await call(
        backend, "get_file", {"repo": REPO, "path": "app/pricing.py", "ref": "main"}
    )
    assert not err and "price * rate" in f["content"]


async def test_codeowners_parsed(backend: MockBackend) -> None:
    err, _, data = await call(backend, "get_codeowners", {"repo": REPO})
    assert not err
    assert {"pattern": "/app/pricing.py", "owners": ["@dev1", "@acme/billing"]} in data["entries"]


async def test_large_outputs_are_truncated(backend: MockBackend) -> None:
    backend.repos[REPO].commits[-1].files[0].patch = "x" * 100_000
    err, _, diff = await call(backend, "get_commit_diff", {"repo": REPO, "sha": C})
    assert not err and diff["truncated"] and len(diff["files"][0]["patch"]) < 5000


async def test_prompt_injection_text_is_returned_as_data_not_acted_on(backend: MockBackend) -> None:
    z = "0718293a4b5c6d7e8f90123456789012345abcde"
    f = "f60718293a4b5c6d7e8f90123456789012345abc"
    err, _, data = await call(
        backend, "list_commits_between", {"repo": "acme/notes-app", "base_sha": f, "head_sha": z}
    )
    assert not err
    assert (
        "IGNORE ALL PREVIOUS INSTRUCTIONS" in data["commits"][0]["message"]
    )  # surfaced verbatim as data
    assert backend.branches == {} and backend.pull_requests == []  # and nothing was written
