"""The real Jira backend against a small fake of Jira Cloud's REST API (no network)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from pd_mcp_jira.__main__ import build_backend
from pd_mcp_jira.adf import adf_text
from pd_mcp_jira.backend import Issue
from pd_mcp_jira.rest import JiraRestBackend, load_user_map
from pd_mcp_jira.server import build_server

BASE = "https://acme.atlassian.net"
TOKEN = "ATATT3x" + "Q" * 30
FP = "fdacb5a1cf4e5ebe"


class FakeJira:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[httpx.Request] = []
        self.reject_assignee = False
        self.fail: Callable[[httpx.Request], httpx.Response | None] = lambda _r: None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if (forced := self.fail(req)) is not None:
            return forced
        p, body = req.url.path, (json.loads(req.content) if req.content else {})
        if req.method == "POST" and p == "/rest/api/3/search/jql":
            m = re.search(r'labels = "([^"]+)"', body["jql"])
            hits = [
                {
                    "id": k,
                    "key": k,
                    "fields": {
                        "summary": i["summary"],
                        "status": i["status"],
                        "labels": i["labels"],
                    },
                }
                for k, i in self.issues.items()
                if m and m.group(1) in i["labels"]
            ]
            return httpx.Response(200, json={"issues": hits, "isLast": True})
        if req.method == "POST" and p == "/rest/api/3/issue":
            f = body["fields"]
            if "assignee" in f and self.reject_assignee:
                return httpx.Response(
                    400, json={"errors": {"assignee": "User cannot be assigned issues."}}
                )
            key = f"DEMO-{len(self.issues) + 1}"
            self.issues[key] = {**f, "status": {"name": "To Do", "statusCategory": {"key": "new"}}}
            return httpx.Response(
                201, json={"id": "1", "key": key, "self": f"{BASE}/rest/api/3/issue/1"}
            )
        if m := re.fullmatch(r"/rest/api/3/issue/([A-Z]+-\d+)/comment", p):
            key = m.group(1)
            if key not in self.issues:
                return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
            if req.method == "GET":
                return httpx.Response(200, json={"comments": self.comments.get(key, [])})
            self.comments.setdefault(key, []).append(
                {"id": str(len(self.comments.get(key, []))), "body": body["body"]}
            )
            return httpx.Response(201, json={"id": "9"})
        return httpx.Response(404, json={"errorMessages": ["nope"]})


def make(fake: FakeJira, **kw: Any) -> JiraRestBackend:
    return JiraRestBackend(
        BASE,
        "svc@acme.example",
        TOKEN,
        "DEMO",
        client=httpx.Client(transport=httpx.MockTransport(fake)),
        sleep=lambda _s: None,
        **kw,
    )


def new_issue(**kw: Any) -> Issue:
    base = {
        "key": "",
        "summary": "[infrastructure] payments-api/main: postgres down",
        "status": "",
        "fingerprint": FP,
        "url": "",
        "description": "h3. Summary\nPostgres is down.\n\n* a\n* b",
        "category": "infrastructure",
        "labels": ["pipeline-doctor"],
    }
    return Issue(**{**base, **kw})


@pytest.fixture
def fake() -> FakeJira:
    return FakeJira()


def test_create_then_find_by_fingerprint(fake: FakeJira) -> None:
    b = make(fake)
    created = b.create_issue(new_issue())
    assert (
        created.key == "DEMO-1"
        and created.url == f"{BASE}/browse/DEMO-1"
        and created.status == "Open"
    )
    stored = fake.issues["DEMO-1"]
    assert stored["project"] == {"key": "DEMO"} and stored["issuetype"] == {"name": "Task"}
    assert f"pd-fp-{FP}" in stored["labels"] and "pipeline-doctor" in stored["labels"]
    assert stored["description"]["type"] == "doc" and "Postgres is down." in adf_text(
        stored["description"]
    )
    found = b.search_by_fingerprint(FP)
    assert [i.key for i in found] == ["DEMO-1"] and found[0].status == "To Do"
    assert b.search_by_fingerprint("0" * 16) == []


def test_done_issues_are_reported_as_closed(fake: FakeJira) -> None:
    b = make(fake)
    b.create_issue(new_issue())
    fake.issues["DEMO-1"]["status"] = {"name": "Shipped", "statusCategory": {"key": "done"}}
    assert b.search_by_fingerprint(FP)[0].status.lower() in {
        "done",
        "closed",
        "resolved",
    }  # any "done" category counts


def test_the_jql_is_constrained_to_the_project_and_cannot_be_injected(fake: FakeJira) -> None:
    b = make(fake)
    for bad in ['x" OR project = OTHER OR labels = "y', "abc", "G" * 16, ""]:
        with pytest.raises(ToolError, match="invalid fingerprint"):
            b.search_by_fingerprint(bad)
    b.search_by_fingerprint(FP)
    jql = json.loads(fake.requests[-1].content)["jql"]
    assert jql.startswith('project = "DEMO" AND labels = "pd-fp-')


def test_assignment_uses_the_reviewed_map_only(fake: FakeJira) -> None:
    b = make(fake, user_map={"dev1": "acct-111", "@Team-Lead": "acct-222"})
    b.create_issue(new_issue(assignee="DEV1"))
    assert fake.issues["DEMO-1"]["assignee"] == {"accountId": "acct-111"}
    b.create_issue(new_issue(assignee="team-lead", fingerprint="a" * 16))
    assert fake.issues["DEMO-2"]["assignee"] == {"accountId": "acct-222"}
    out = b.create_issue(new_issue(assignee="stranger", fingerprint="b" * 16))
    assert (
        "assignee" not in fake.issues["DEMO-3"] and out.assignee is None
    )  # unknown handle: unassigned, not guessed


def test_a_rejected_assignee_does_not_lose_the_ticket(fake: FakeJira) -> None:
    fake.reject_assignee = True
    out = make(fake, user_map={"dev1": "acct-111"}).create_issue(new_issue(assignee="dev1"))
    assert out.key == "DEMO-1" and "assignee" not in fake.issues["DEMO-1"] and out.assignee is None
    assert (
        sum(1 for r in fake.requests if r.method == "POST") == 2
    )  # one retry, without the assignee


def test_comments_are_idempotent_by_marker(fake: FakeJira) -> None:
    b = make(fake)
    b.create_issue(new_issue())
    marker = "[pd-occurrence:payments-api/main:31]"
    assert b.add_comment("DEMO-1", f"Seen again.\n\n{marker}", marker) is True
    assert b.add_comment("DEMO-1", f"Seen again.\n\n{marker}", marker) is False
    assert len(fake.comments["DEMO-1"]) == 1
    assert b.add_comment("DEMO-1", "Another one", None) is True


def test_only_issues_of_the_configured_project_are_commentable(fake: FakeJira) -> None:
    b = make(fake)
    for key in ("OTHER-1", "DEMO", "demo-1", "DEMO-1; DROP"):
        with pytest.raises(ToolError, match="not in project"):
            b.add_comment(key, "x")
    assert fake.requests == []


def test_hostile_description_text_reaches_jira_only_as_inert_nodes(fake: FakeJira) -> None:
    make(fake).create_issue(
        new_issue(description="Evidence: {code}x{code} [~accountid:1] [a|javascript:alert(1)]")
    )
    sent = json.dumps(fake.issues["DEMO-1"]["description"])
    assert (
        "mention" not in sent
        and "javascript:alert(1)" in adf_text(fake.issues["DEMO-1"]["description"])
        and '"href"' not in sent
    )


@pytest.mark.parametrize(
    ("response", "expect"),
    [
        (httpx.Response(401, json={}), "rejected the credentials"),
        (httpx.Response(403, json={}), "lacks permission"),
        (httpx.Response(404, json={}), "not found"),
        (httpx.Response(429, headers={"retry-after": "30"}, json={}), "rate limit"),
        (httpx.Response(400, json={"errors": {"summary": "too long"}}), "invalid"),
        (httpx.Response(500, json={}), "Jira error 500"),
        (httpx.Response(200, content=b"<html>"), "not JSON"),
    ],
)
def test_failures_become_clear_errors_without_credentials(
    fake: FakeJira, response: httpx.Response, expect: str
) -> None:
    fake.fail = lambda _r: response
    with pytest.raises(ToolError, match=expect) as info:
        make(fake).search_by_fingerprint(FP)
    assert TOKEN not in str(info.value) and "svc@acme.example" not in str(info.value)


def test_reads_retry_but_writes_do_not(fake: FakeJira) -> None:
    n = {"c": 0}

    def flaky(_r: httpx.Request) -> httpx.Response | None:
        n["c"] += 1
        return httpx.Response(503, json={}) if n["c"] == 1 else None

    fake.fail = flaky
    assert make(fake).search_by_fingerprint(FP) == [] and n["c"] == 2
    fake.fail = lambda r: httpx.Response(503, json={}) if r.url.path.endswith("/issue") else None
    with pytest.raises(ToolError, match="503"):
        make(fake).create_issue(new_issue())
    assert sum(1 for r in fake.requests if r.url.path.endswith("/issue")) == 1


def test_requests_use_basic_auth_over_https_and_never_put_secrets_in_urls(fake: FakeJira) -> None:
    make(fake).search_by_fingerprint(FP)
    r = fake.requests[0]
    assert (
        r.headers["authorization"].startswith("Basic ")
        and TOKEN not in str(r.url)
        and r.url.scheme == "https"
    )


@pytest.mark.parametrize(
    ("args", "match"),
    [
        (("https://x", "", TOKEN, "DEMO"), "credentials"),
        (("https://x", "a@b", " ", "DEMO"), "credentials"),
        (("http://acme.atlassian.net", "a@b", TOKEN, "DEMO"), "https"),
        (("https://x", "a@b", TOKEN, "demo"), "PROJECT"),
        (("https://x", "a@b", TOKEN, 'DEMO" OR 1=1'), "PROJECT"),
    ],
)
def test_unsafe_configuration_is_rejected_at_start(
    args: tuple[str, str, str, str], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        JiraRestBackend(*args)


def test_user_map_file_and_startup_wiring(tmp_path: Path) -> None:
    f = tmp_path / "users.json"
    f.write_text(json.dumps({"@Dev1": "acct-1"}))
    assert load_user_map(str(f)) == {"dev1": "acct-1"}
    f.write_text(json.dumps(["not", "a", "map"]))
    with pytest.raises(ValueError, match="handle -> account id"):
        load_user_map(str(f))
    env = {
        "MCP_MODE": "real",
        "JIRA_BASE_URL": BASE,
        "JIRA_EMAIL": "a@b.c",
        "JIRA_API_TOKEN": TOKEN,
        "JIRA_PROJECT_KEY": "DEMO",
    }
    assert isinstance(build_backend(env), JiraRestBackend)
    with pytest.raises(ValueError, match="credentials") as info:
        build_backend({**env, "JIRA_API_TOKEN": ""})
    assert TOKEN not in str(info.value)
    with pytest.raises(ValueError, match="mock or real"):
        build_backend({"MCP_MODE": "prod"})


async def test_the_tools_work_end_to_end_on_the_rest_backend(fake: FakeJira) -> None:
    async with Client(build_server(make(fake))) as c:
        args = {
            "summary": "[infrastructure] payments-api/main: postgres down",
            "description": "h3. Summary\nDown.",
            "fingerprint": FP,
            "category": "infrastructure",
        }
        created = await c.call_tool("create_issue", args)
        assert not created.is_error and created.structured_content["key"] == "DEMO-1"
        dup = await c.call_tool(
            "create_issue", args
        )  # the open issue is found through Jira, so no duplicate
        assert dup.is_error and "already tracks" in "".join(
            getattr(x, "text", "") for x in dup.content
        )
        c1 = await c.call_tool(
            "add_comment",
            {"issue_key": "DEMO-1", "body": "again", "idempotency_key": "payments-api/main:31"},
        )
        c2 = await c.call_tool(
            "add_comment",
            {"issue_key": "DEMO-1", "body": "again", "idempotency_key": "payments-api/main:31"},
        )
        assert (
            c1.structured_content["duplicate"] is False
            and c2.structured_content["duplicate"] is True
        )
