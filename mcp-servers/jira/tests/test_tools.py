from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp.client import Client
from pd_mcp_jira.backend import MockBackend
from pd_mcp_jira.server import build_server

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
INFRA_FP = "fdacb5a1cf4e5ebe"


@pytest.fixture
def backend() -> MockBackend:
    return MockBackend.load(FIXTURES)


async def call(backend: MockBackend, tool: str, args: dict[str, Any]) -> tuple[bool, str, Any]:
    async with Client(build_server(backend)) as client:
        res = await client.call_tool(tool, args)
    return (
        bool(res.is_error),
        "".join(getattr(c, "text", "") for c in res.content),
        res.structured_content,
    )


async def test_tool_surface_has_no_project_argument(backend: MockBackend) -> None:
    async with Client(build_server(backend)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"search_similar_issues", "create_issue", "add_comment"}
    for t in tools.values():
        assert "project" not in t.input_schema.get(
            "properties", {}
        )  # scoped to one project by config


async def test_search_finds_open_issue_by_fingerprint(backend: MockBackend) -> None:
    err, _, data = await call(backend, "search_similar_issues", {"fingerprint": INFRA_FP})
    assert not err
    assert [(i["key"], i["open"]) for i in data["issues"]] == [("DEMO-7", True)]


async def test_search_reports_closed_issue_as_not_open(backend: MockBackend) -> None:
    _, _, data = await call(backend, "search_similar_issues", {"fingerprint": "1111aaaa2222bbbb"})
    assert data["issues"][0]["open"] is False


async def test_search_no_match(backend: MockBackend) -> None:
    _, _, data = await call(backend, "search_similar_issues", {"fingerprint": "0" * 16})
    assert data["issues"] == []


@pytest.mark.parametrize("fp", ["short", "XYZ" * 8, "fdacb5a1cf4e5ebe; DROP", "9F2C1A7B3D4E5F60"])
async def test_search_rejects_bad_fingerprint(backend: MockBackend, fp: str) -> None:
    err, text, _ = await call(backend, "search_similar_issues", {"fingerprint": fp})
    assert err and "pattern" in text


async def test_create_issue_and_dedupe_guard(backend: MockBackend) -> None:
    args = {
        "summary": "Discount calculation broken",
        "description": "Root cause: ...",
        "fingerprint": "abcdef0123456789",
        "category": "code",
        "assignee": "dev1",
    }
    err, _, created = await call(backend, "create_issue", args)
    assert not err and created["key"].startswith("DEMO-")
    stored = backend.issues[created["key"]]
    assert (
        stored.status == "Open" and stored.assignee == "dev1" and "pipeline-doctor" in stored.labels
    )

    err, text, _ = await call(
        backend, "create_issue", args
    )  # same fingerprint, open issue now exists
    assert err and created["key"] in text and "add_comment" in text


async def test_create_issue_refuses_duplicate_of_seeded_open_issue(backend: MockBackend) -> None:
    err, text, _ = await call(
        backend,
        "create_issue",
        {
            "summary": "Postgres unreachable",
            "description": "d",
            "fingerprint": INFRA_FP,
            "category": "infrastructure",
        },
    )
    assert err and "DEMO-7" in text
    assert len(backend.issues) == 2


async def test_create_issue_allowed_when_previous_is_closed(backend: MockBackend) -> None:
    err, _, _ = await call(
        backend,
        "create_issue",
        {
            "summary": "Login test flaky again",
            "description": "d",
            "fingerprint": "1111aaaa2222bbbb",
            "category": "flaky",
        },
    )
    assert not err


@pytest.mark.parametrize(
    "patch",
    [
        {"category": "magic"},
        {"summary": "x"},
        {"summary": "y" * 300},
        {"assignee": "a b; c"},
        {"description": ""},
    ],
)
async def test_create_issue_validation(backend: MockBackend, patch: dict[str, str]) -> None:
    args = {
        "summary": "A valid summary",
        "description": "d",
        "fingerprint": "abcdef0123456789",
        "category": "code",
        **patch,
    }
    err, _, _ = await call(backend, "create_issue", args)
    assert err and len(backend.issues) == 2


async def test_add_comment(backend: MockBackend) -> None:
    err, _, data = await call(
        backend, "add_comment", {"issue_key": "DEMO-7", "body": "Seen again in build 31."}
    )
    assert not err and data["ok"] is True
    assert backend.issues["DEMO-7"].comments == ["Seen again in build 31."]


async def test_add_comment_is_idempotent_with_a_key(backend: MockBackend) -> None:
    args = {
        "issue_key": "DEMO-7",
        "body": "Seen again in build 31.",
        "idempotency_key": "payments-api/main:31",
    }
    err, _, first = await call(backend, "add_comment", args)
    err2, _, second = await call(backend, "add_comment", args)
    assert not err and not err2
    assert first["duplicate"] is False and second["duplicate"] is True
    assert len(backend.issues["DEMO-7"].comments) == 1
    assert "[pd-occurrence:payments-api/main:31]" in backend.issues["DEMO-7"].comments[0]
    _, _, other = await call(
        backend, "add_comment", {**args, "idempotency_key": "payments-api/main:32"}
    )
    assert other["duplicate"] is False and len(backend.issues["DEMO-7"].comments) == 2


async def test_unknown_category_is_accepted_for_fallback_tickets(backend: MockBackend) -> None:
    err, _, _ = await call(
        backend,
        "create_issue",
        {
            "summary": "Automatic diagnosis unavailable",
            "description": "d",
            "fingerprint": "abcdef0123456789",
            "category": "unknown",
        },
    )
    assert not err


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"issue_key": "DEMO-999", "body": "x"}, "not found"),
        ({"issue_key": "demo-7", "body": "x"}, "pattern"),
        ({"issue_key": "DEMO-7", "body": ""}, "at least 1"),
        ({"issue_key": "DEMO-7", "body": "x" * 20001}, "at most"),
    ],
)
async def test_add_comment_rejections(
    backend: MockBackend, args: dict[str, str], needle: str
) -> None:
    err, text, _ = await call(backend, "add_comment", args)
    assert err and needle in text
