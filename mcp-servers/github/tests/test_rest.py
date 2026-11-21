"""The real GitHub backend, against a small fake of GitHub's REST API (no network).

The fake follows GitHub's documented response shapes. It is built from the same fixtures as the
mock backend so the two can be compared: the tools must behave the same on both.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from pd_mcp_github.__main__ import build_backend
from pd_mcp_github.backend import MockBackend, RawRepo
from pd_mcp_github.rest import NotFound, RestBackend
from pd_mcp_github.server import build_server

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
REPO = "acme/demo-service"
A = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
B = "b2c3d4e5f60718293a4b5c6d7e8f901234567890"
C = "c3d4e5f60718293a4b5c6d7e8f90123456789012"
TOKEN = "github_pat_" + "X" * 40


class FakeGitHub:
    """Enough of the GitHub REST API for the backend; records every request."""

    def __init__(self) -> None:
        self.repo: RawRepo = MockBackend.load(FIXTURES).repos[REPO]
        self.requests: list[httpx.Request] = []
        self.branches: dict[str, list[str]] = {}  # name -> commits beyond its base, newest last
        self.branch_base: dict[str, str] = {}
        self.branch_files: dict[str, dict[str, str]] = {}
        self.prs: list[dict[str, Any]] = []
        self.fail: Callable[[httpx.Request], httpx.Response | None] = lambda _r: None
        self.allow_draft = True

    # -- helpers
    def idx(self, ref: str) -> int:
        ref = ref.lower()
        if ref in self.branch_base:  # a branch: resolves to its base for ordering purposes
            return self.idx(self.branch_base[ref])
        hits = [i for i, c in enumerate(self.repo.commits) if c.sha.startswith(ref)]
        if len(hits) != 1:
            raise KeyError(ref)
        return hits[0]

    def commit_json(self, i: int, files: bool = False) -> dict[str, Any]:
        c = self.repo.commits[i]
        body: dict[str, Any] = {
            "sha": c.sha,
            "commit": {"author": {"name": c.author, "date": c.date}, "message": c.message},
            "author": {"login": c.author},
        }
        if files:
            body["files"] = [
                {"filename": f.path, "status": f.status, **({"patch": f.patch} if f.patch else {})}
                for f in c.files
            ]
        return body

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if (forced := self.fail(req)) is not None:
            return forced
        path = req.url.path
        m = re.fullmatch(rf"/repos/{REPO}(/.*)?", path)
        if not m:
            return httpx.Response(404, json={"message": "Not Found"})
        sub = m.group(1) or ""
        try:
            return self.route(req, sub)
        except KeyError:
            return httpx.Response(404, json={"message": "Not Found"})

    def route(self, req: httpx.Request, sub: str) -> httpx.Response:
        j = json.loads(req.content) if req.content else {}
        q = dict(req.url.params)
        if req.method == "GET" and sub == "":
            return httpx.Response(200, json={"default_branch": "main"})
        if req.method == "GET" and (m := re.fullmatch(r"/compare/(.+)\.\.\.(.+)", sub)):
            b, h = m.group(1), m.group(2)
            if h in self.branches:  # compare to a branch we created
                n = len(self.branches[h])
                return httpx.Response(
                    200,
                    json={
                        "status": "ahead" if n else "identical",
                        "ahead_by": n,
                        "commits": [],
                        "total_commits": n,
                    },
                )
            bi, hi = self.idx(b), self.idx(h)
            status = "ahead" if hi > bi else ("identical" if hi == bi else "behind")
            commits = [self.commit_json(i) for i in range(bi + 1, hi + 1)]
            return httpx.Response(
                200,
                json={
                    "status": status,
                    "ahead_by": len(commits),
                    "commits": commits,
                    "total_commits": len(commits),
                },
            )
        if req.method == "GET" and (m := re.fullmatch(r"/commits/(.+)", sub)):
            return httpx.Response(200, json=self.commit_json(self.idx(m.group(1)), files=True))
        if req.method == "GET" and (m := re.fullmatch(r"/contents/(.+)", sub)):
            path, ref = m.group(1), q.get("ref", "main")
            text = self.branch_files.get(ref, {}).get(path)
            if text is None:
                text = next((f.content for f in self.repo.files if f.path == path), None)
            if text is None and path.endswith("CODEOWNERS") and path == ".github/CODEOWNERS":
                text = self.repo.codeowners or None
            if text is None:
                return httpx.Response(404, json={"message": "Not Found"})
            enc = base64.b64encode(text.encode()).decode()
            return httpx.Response(
                200, json={"type": "file", "encoding": "base64", "content": enc, "path": path}
            )
        if req.method == "POST" and sub == "/git/refs":
            name = j["ref"].removeprefix("refs/heads/")
            if name in self.branches:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.branches[name] = []
            self.branch_base[name] = j["sha"]
            self.branch_files[name] = {}
            return httpx.Response(201, json={"ref": j["ref"], "object": {"sha": j["sha"]}})
        if req.method == "GET" and (m := re.fullmatch(r"/git/ref/heads/(.+)", sub)):
            return httpx.Response(200, json={"object": {"sha": self.head_of(m.group(1))}})
        if req.method == "GET" and (m := re.fullmatch(r"/git/commits/(.+)", sub)):
            return httpx.Response(200, json={"tree": {"sha": "tree-" + m.group(1)[:8]}})
        if req.method == "POST" and sub == "/git/trees":
            self.pending_tree = {t["path"]: t["content"] for t in j["tree"]}
            return httpx.Response(201, json={"sha": "newtree"})
        if req.method == "POST" and sub == "/git/commits":
            sha = f"{len(self.requests):040x}"
            self.pending_commit = sha
            return httpx.Response(201, json={"sha": sha})
        if req.method == "PATCH" and (m := re.fullmatch(r"/git/refs/heads/(.+)", sub)):
            name = m.group(1)
            self.branches[name].append(self.pending_commit)
            self.branch_files[name].update(self.pending_tree)
            return httpx.Response(200, json={"object": {"sha": self.pending_commit}})
        if req.method == "GET" and sub == "/pulls":
            head = q["head"].split(":", 1)[1]
            return httpx.Response(200, json=[p for p in self.prs if p["head_name"] == head])
        if req.method == "POST" and sub == "/pulls":
            if not self.allow_draft:
                return httpx.Response(
                    422,
                    json={"message": "Draft pull requests are not supported in this repository"},
                )
            n = len(self.prs) + 1
            pr = {
                "number": n,
                "html_url": f"https://github.com/{REPO}/pull/{n}",
                "draft": j["draft"],
                "head_name": j["head"],
                **j,
            }
            self.prs.append(pr)
            return httpx.Response(201, json=pr)
        return httpx.Response(404, json={"message": "Not Found"})

    def head_of(self, name: str) -> str:
        if name not in self.branches:
            raise KeyError(name)
        return (self.branches[name] or [self.branch_base[name]])[-1]


def make(fake: FakeGitHub, **kw: Any) -> RestBackend:
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return RestBackend(TOKEN, frozenset({REPO}), client=client, sleep=lambda _s: None, **kw)


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def rest(fake: FakeGitHub) -> RestBackend:
    return make(fake)


# ---- reads: same answers as the mock ------------------------------------------------------------


def test_reads_match_the_mock_backend(rest: RestBackend) -> None:
    mock = MockBackend.load(FIXTURES)
    assert rest.default_branch(REPO) == mock.default_branch(REPO) == "main"
    assert [(c.sha, c.author, c.message) for c in rest.commits_between(REPO, A, C)] == [
        (c.sha, c.author, c.message) for c in mock.commits_between(REPO, A, C)
    ]
    assert [f.path for f in rest.commits_between(REPO, A, C)[-1].files] == ["app/pricing.py"]
    assert rest.commit(REPO, C[:10]).files[0].patch == mock.commit(REPO, C).files[0].patch
    assert rest.file(REPO, "app/pricing.py", C) == mock.file(REPO, "app/pricing.py", C)


def test_codeowners_found_in_the_standard_location(rest: RestBackend) -> None:
    assert "@dev1" in rest.codeowners(REPO)


def test_a_wrong_order_range_is_refused_like_the_mock(rest: RestBackend) -> None:
    with pytest.raises(ToolError, match="ancestor"):
        rest.commits_between(REPO, C, A)


def test_unknown_commit_and_file_give_helpful_errors(rest: RestBackend) -> None:
    with pytest.raises(ToolError, match="not found"):
        rest.commit(REPO, "0" * 40)
    with pytest.raises(NotFound, match="not found"):
        rest.file(REPO, "nope.py", "main")


# ---- the repository allowlist ----------------------------------------------------------------------


@pytest.mark.parametrize("repo", ["evil/other", "acme/payments-api", "ACME/DEMO-SERVICE-x"])
def test_other_repositories_are_refused_before_any_request(fake: FakeGitHub, repo: str) -> None:
    rest = make(fake)
    for call in (
        lambda: rest.default_branch(repo),
        lambda: rest.commit(repo, C),
        lambda: rest.file(repo, "a", "main"),
        lambda: rest.create_branch(repo, "pipeline-doctor/x", C),
        lambda: rest.open_pull_request(repo, "h", "main", "t", "b"),
    ):
        with pytest.raises(ToolError, match="not found or not accessible"):
            call()
    assert fake.requests == []


def test_the_allowlist_is_case_insensitive_like_github() -> None:
    rest = RestBackend(
        TOKEN,
        frozenset({"Acme/Demo-Service"}),
        client=httpx.Client(transport=httpx.MockTransport(FakeGitHub())),
    )
    assert rest.default_branch("acme/demo-service") == "main"


# ---- writes -------------------------------------------------------------------------------------------


def test_branch_commit_and_draft_pull_request_flow(fake: FakeGitHub, rest: RestBackend) -> None:
    rest.create_branch(REPO, "pipeline-doctor/fix-1", C)
    sha = rest.commit_files(
        REPO, "pipeline-doctor/fix-1", "Fix discount", {"app/pricing.py": "fixed\n"}
    )
    assert re.fullmatch(r"[0-9a-f]{40}", sha)
    number, url = rest.open_pull_request(REPO, "pipeline-doctor/fix-1", "main", "Fix", "body")
    assert (number, url) == (1, f"https://github.com/{REPO}/pull/1")
    assert fake.prs[0]["draft"] is True and fake.prs[0]["maintainer_can_modify"] is False
    assert rest.open_pull_request(REPO, "pipeline-doctor/fix-1", "main", "Fix", "body") == (
        number,
        url,
    )
    assert len(fake.prs) == 1  # idempotent


def test_duplicate_branch_is_a_clear_error(rest: RestBackend) -> None:
    rest.create_branch(REPO, "pipeline-doctor/x", C)
    with pytest.raises(ToolError, match="already exists"):
        rest.create_branch(REPO, "pipeline-doctor/x", C)


def test_new_files_are_refused(rest: RestBackend) -> None:
    rest.create_branch(REPO, "pipeline-doctor/x", C)
    with pytest.raises(ToolError, match="new files are not allowed"):
        rest.commit_files(REPO, "pipeline-doctor/x", "m", {"brand_new.py": "x"})


def test_a_pull_request_without_commits_is_refused(rest: RestBackend) -> None:
    rest.create_branch(REPO, "pipeline-doctor/x", C)
    with pytest.raises(ToolError, match="no changes"):
        rest.open_pull_request(REPO, "pipeline-doctor/x", "main", "t", "b")


def test_if_github_cannot_make_a_draft_nothing_else_is_created(
    fake: FakeGitHub, rest: RestBackend
) -> None:
    rest.create_branch(REPO, "pipeline-doctor/x", C)
    rest.commit_files(REPO, "pipeline-doctor/x", "m", {"app/pricing.py": "y\n"})
    fake.allow_draft = False
    with pytest.raises(ToolError, match="Draft pull requests are not supported"):
        rest.open_pull_request(REPO, "pipeline-doctor/x", "main", "t", "b")
    assert fake.prs == []  # never falls back to a ready-for-review pull request


def test_a_non_draft_answer_from_github_is_not_accepted(
    fake: FakeGitHub, rest: RestBackend
) -> None:
    rest.create_branch(REPO, "pipeline-doctor/x", C)
    rest.commit_files(REPO, "pipeline-doctor/x", "m", {"app/pricing.py": "y\n"})
    orig = fake.route

    def lying(req: httpx.Request, sub: str) -> httpx.Response:
        r = orig(req, sub)
        if req.method == "POST" and sub == "/pulls":
            return httpx.Response(201, json={**r.json(), "draft": False})
        return r

    fake.route = lying  # type: ignore[method-assign]
    with pytest.raises(ToolError, match="did not create a draft"):
        rest.open_pull_request(REPO, "pipeline-doctor/x", "main", "t", "b")


# ---- failure handling and secrecy ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expect"),
    [
        (httpx.Response(401, json={"message": "Bad credentials"}), "rejected the credentials"),
        (
            httpx.Response(
                403, json={"message": "Resource not accessible by personal access token"}
            ),
            "lacks permission",
        ),
        (
            httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"},
                json={"message": "rate limit"},
            ),
            "rate limit",
        ),
        (httpx.Response(429, headers={"retry-after": "30"}, json={}), "rate limit"),
        (httpx.Response(500, json={"message": "boom"}), "GitHub error 500"),
        (httpx.Response(200, content=b"<html>not json"), "not JSON"),
    ],
)
def test_failures_become_clear_tool_errors_without_the_token(
    fake: FakeGitHub, response: httpx.Response, expect: str
) -> None:
    fake.fail = lambda _r: response
    with pytest.raises(ToolError, match=expect) as info:
        make(fake).default_branch(REPO)
    assert TOKEN not in str(info.value)


def test_reads_are_retried_on_a_transient_error_but_writes_are_not(fake: FakeGitHub) -> None:
    calls = {"n": 0}

    def flaky(_r: httpx.Request) -> httpx.Response | None:
        calls["n"] += 1
        return httpx.Response(503, json={}) if calls["n"] == 1 else None

    fake.fail = flaky
    assert make(fake).default_branch(REPO) == "main" and calls["n"] == 2

    fake.fail = lambda r: httpx.Response(503, json={}) if r.method == "POST" else None
    rest = make(fake)
    with pytest.raises(ToolError, match="503"):
        rest.create_branch(REPO, "pipeline-doctor/x", C)
    assert sum(1 for r in fake.requests if r.method == "POST") == 1


def test_a_timeout_and_a_connection_error_are_reported_not_raised_raw(fake: FakeGitHub) -> None:
    def boom(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
        def handler(_r: httpx.Request) -> httpx.Response:
            raise exc

        return handler

    for exc, text in (
        (httpx.ReadTimeout("t"), "in time"),
        (httpx.ConnectError("c"), "could not reach"),
    ):
        rest = RestBackend(
            TOKEN,
            frozenset({REPO}),
            client=httpx.Client(transport=httpx.MockTransport(boom(exc))),
            sleep=lambda _s: None,
        )
        with pytest.raises(ToolError, match=text):
            rest.default_branch(REPO)


def test_every_request_is_authenticated_pinned_and_never_leaks_the_token_into_the_url(
    fake: FakeGitHub, rest: RestBackend
) -> None:
    rest.commit(REPO, C)
    for r in fake.requests:
        assert r.headers["authorization"] == f"Bearer {TOKEN}"
        assert r.headers["x-github-api-version"] == "2022-11-28"
        assert TOKEN not in str(r.url)
        assert r.url.host == "api.github.com"


@pytest.mark.parametrize(
    ("token", "repos", "url"),
    [
        ("", {REPO}, "https://api.github.com"),
        ("  ", {REPO}, "https://api.github.com"),
        (TOKEN, set(), "https://api.github.com"),
        (TOKEN, {REPO}, "http://api.github.com"),
        (TOKEN, {REPO}, "ftp://x"),
    ],
)
def test_unsafe_configuration_is_rejected_at_start(token: str, repos: set[str], url: str) -> None:
    with pytest.raises(ValueError, match=r"required|https"):
        RestBackend(token, frozenset(repos), api_url=url)


def test_startup_wiring_reads_the_environment_and_never_echoes_the_token() -> None:
    backend = build_backend(
        {"MCP_MODE": "real", "GITHUB_TOKEN": TOKEN, "GITHUB_ALLOWED_REPOS": f" {REPO} , other/x "}
    )
    assert isinstance(backend, RestBackend) and backend.allowed == {REPO, "other/x"}
    with pytest.raises(ValueError, match="allowed repository") as info:
        build_backend({"MCP_MODE": "real", "GITHUB_TOKEN": TOKEN})
    assert TOKEN not in str(info.value)
    with pytest.raises(ValueError, match="mock or real"):
        build_backend({"MCP_MODE": "prod"})


# ---- through the MCP tools, same as the mock -----------------------------------------------------------------


async def test_the_tools_work_end_to_end_on_the_rest_backend(
    fake: FakeGitHub, rest: RestBackend
) -> None:
    async with Client(build_server(rest)) as client:
        listed = await client.call_tool(
            "list_commits_between", {"repo": REPO, "base_sha": A, "head_sha": C}
        )
        assert not listed.is_error and [c["sha"] for c in listed.structured_content["commits"]] == [
            B,
            C,
        ]
        branch = await client.call_tool(
            "create_branch", {"repo": REPO, "name": "pipeline-doctor/fix-2", "from_sha": C}
        )
        assert not branch.is_error
        commit = await client.call_tool(
            "commit_files",
            {
                "repo": REPO,
                "branch": "pipeline-doctor/fix-2",
                "message": "Fix",
                "files": [{"path": "app/pricing.py", "content": "ok\n"}],
            },
        )
        assert not commit.is_error
        pr = await client.call_tool(
            "open_draft_pull_request",
            {
                "repo": REPO,
                "head": "pipeline-doctor/fix-2",
                "base": "main",
                "title": "Fix",
                "body": "x",
            },
        )
        assert not pr.is_error and pr.structured_content["draft"] is True
        # server-side policy still applies on top of the backend
        protected = await client.call_tool(
            "commit_files",
            {
                "repo": REPO,
                "branch": "pipeline-doctor/fix-2",
                "message": "x",
                "files": [{"path": ".github/workflows/ci.yml", "content": "x"}],
            },
        )
        assert protected.is_error
        main = await client.call_tool(
            "create_branch", {"repo": REPO, "name": "main", "from_sha": C}
        )
        assert main.is_error
