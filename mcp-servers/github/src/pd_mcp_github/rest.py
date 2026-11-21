"""GitHub REST backend (real mode).

Everything the tools need, over GitHub's REST API with one fine-grained token. Guard rails that do
not depend on the tool layer above:

* **Repository allowlist.** Any repository not named in ``allowed_repos`` is refused before a
  request is made, so even a bug in scope pinning cannot reach another repository.
* **No new files.** ``commit_files`` only updates files that already exist on the branch.
* **Draft only.** A pull request is created as a draft or not at all; if GitHub cannot make one
  (some plans refuse drafts on private repositories) the call fails, it never falls back.
* **No secrets in messages.** Errors carry a status class and a short, control-character-free
  summary; the token and request headers are never included.
* **Bounded.** Request timeout, response size cap, and a small retry only for reads.

The token should be a fine-grained personal access token limited to the test repositories with
``Contents: read/write``, ``Pull requests: read/write`` and ``Metadata: read``. Nothing else.
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from pd_mcp_github.backend import RawCommit, RawFile

DEFAULT_API = "https://api.github.com"
TIMEOUT_S = 15.0
MAX_BODY = 5_000_000
MAX_COMMIT_DETAILS = 20  # per-commit file lists fetched for a range; later commits list no files
RETRY_STATUS = {502, 503, 504}
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}


class NotFound(ToolError):
    """404 from GitHub; separate so callers can tell "absent" from "broken"."""


def _short(text: object, limit: int = 200) -> str:
    return _CONTROL.sub(" ", str(text))[:limit]


class RestBackend:
    def __init__(
        self,
        token: str,
        allowed_repos: frozenset[str],
        *,
        api_url: str = DEFAULT_API,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token.strip():
            raise ValueError("a GitHub token is required (GITHUB_TOKEN)")
        if not allowed_repos:
            raise ValueError("at least one allowed repository is required (GITHUB_ALLOWED_REPOS)")
        url = urlparse(api_url)
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in _LOCAL_HOSTS):
            raise ValueError("GITHUB_API_URL must use https")
        self.allowed = frozenset(r.lower() for r in allowed_repos)
        self.api = api_url.rstrip("/")
        self._token = token
        self._client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
        self._sleep = sleep
        self._default_branch: dict[str, str] = {}

    # -- plumbing ----------------------------------------------------------------------------
    def _gate(self, repo: str) -> None:
        if repo.lower() not in self.allowed:
            raise ToolError(
                f"repository '{repo}' not found or not accessible; "
                "use the repo from the PipelineFailed event"
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                resp = self._client.request(
                    method,
                    self.api + path,
                    params=params,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "pipeline-doctor",
                    },
                    timeout=TIMEOUT_S,
                )
            except httpx.TimeoutException:
                raise ToolError("GitHub did not answer in time; try again later") from None
            except httpx.HTTPError as exc:
                raise ToolError(f"could not reach GitHub ({type(exc).__name__})") from None
            if resp.status_code in RETRY_STATUS and attempt + 1 < attempts:
                self._sleep(0.5 * (attempt + 1))
                continue
            return self._decode(resp)
        raise AssertionError("unreachable")  # pragma: no cover

    def _decode(self, resp: httpx.Response) -> Any:
        code = resp.status_code
        if len(resp.content) > MAX_BODY:
            raise ToolError("GitHub's response was too large to handle")
        if code >= 400:
            raise self._error(resp)
        if code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise ToolError("GitHub returned something that is not JSON") from None

    def _error(self, resp: httpx.Response) -> ToolError:
        code = resp.status_code
        try:
            message = _short(resp.json().get("message", ""))
        except (ValueError, AttributeError):
            message = ""
        if code == 401:
            return ToolError("GitHub rejected the credentials (token missing, expired or revoked)")
        if code == 403 or code == 429:
            if resp.headers.get("x-ratelimit-remaining") == "0" or resp.headers.get("retry-after"):
                wait = resp.headers.get("retry-after") or resp.headers.get("x-ratelimit-reset", "?")
                return ToolError(f"GitHub rate limit reached (retry after {_short(wait, 20)})")
            return ToolError(f"GitHub refused the request: the token lacks permission ({message})")
        if code == 404:
            return NotFound(
                "not found, or the token cannot see it; check the repository, SHA, path and ref"
            )
        if code == 409:
            return ToolError(f"conflict: {message}")
        if code == 422:
            return ToolError(f"GitHub rejected the request as invalid: {message}")
        return ToolError(f"GitHub error {code}")

    @staticmethod
    def _p(path: str) -> str:
        return quote(path, safe="/")

    # -- reads -------------------------------------------------------------------------------
    def default_branch(self, repo: str) -> str:
        self._gate(repo)
        if repo not in self._default_branch:
            data = self._request("GET", f"/repos/{repo}")
            self._default_branch[repo] = str(data["default_branch"])
        return self._default_branch[repo]

    def commits_between(self, repo: str, base: str, head: str) -> list[RawCommit]:
        self._gate(repo)
        try:
            data = self._request(
                "GET", f"/repos/{repo}/compare/{base}...{head}", params={"per_page": "100"}
            )
        except NotFound:
            raise ToolError(
                f"commit '{base}' or '{head}' not found in {repo}; "
                "use SHAs from get_build or get_last_successful_build"
            ) from None
        if data.get("status") != "ahead":
            raise ToolError(
                "base_sha must be an ancestor of head_sha: pass the last green commit as base "
                "and the failing commit as head"
            )
        listed = data.get("commits", [])
        if int(data.get("total_commits", len(listed))) > len(listed):
            raise ToolError(
                f"the range has {data['total_commits']} commits, too many to list; "
                "narrow it with a closer base commit"
            )
        out: list[RawCommit] = []
        for i, c in enumerate(listed):
            files: list[RawFile] = []
            if i < MAX_COMMIT_DETAILS:
                files = self.commit(repo, c["sha"]).files
            out.append(_commit(c, files))
        return out

    def commit(self, repo: str, sha: str) -> RawCommit:
        self._gate(repo)
        try:
            c = self._request("GET", f"/repos/{repo}/commits/{sha}")
        except NotFound:
            raise ToolError(
                f"commit '{sha}' not found in {repo} (or ambiguous); "
                "use SHAs from get_build or get_last_successful_build"
            ) from None
        files = [
            RawFile(
                path=str(f["filename"]),
                status=str(f.get("status", "modified")),
                patch=f.get("patch", ""),
            )
            for f in c.get("files", [])
        ]
        return _commit(c, files)

    def file(self, repo: str, path: str, ref: str) -> str:
        self._gate(repo)
        try:
            data = self._request(
                "GET", f"/repos/{repo}/contents/{self._p(path)}", params={"ref": ref}
            )
        except NotFound:
            raise NotFound(f"file '{path}' not found at ref '{ref}' in {repo}") from None
        if not isinstance(data, dict) or data.get("type") != "file":
            raise ToolError(f"'{path}' is not a regular file")
        if data.get("encoding") != "base64":
            raise ToolError(f"'{path}' is too large to read through the API")
        try:
            return base64.b64decode(data.get("content", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise ToolError(f"'{path}' is not a text file") from None

    def codeowners(self, repo: str) -> str:
        branch = self.default_branch(repo)
        for path in CODEOWNERS_PATHS:
            try:
                return self.file(repo, path, branch)
            except NotFound:
                continue
        raise ToolError(f"{repo} has no CODEOWNERS file")

    # -- writes ------------------------------------------------------------------------------
    def create_branch(self, repo: str, name: str, from_sha: str) -> str:
        self._gate(repo)
        full = str(self.commit_sha(repo, from_sha))
        try:
            self._request(
                "POST", f"/repos/{repo}/git/refs", body={"ref": f"refs/heads/{name}", "sha": full}
            )
        except ToolError as exc:
            if "already exists" in str(exc).lower():
                raise ToolError(
                    f"branch '{name}' already exists in {repo}; choose a different name"
                ) from None
            raise
        return full

    def commit_sha(self, repo: str, sha: str) -> str:
        try:
            return str(self._request("GET", f"/repos/{repo}/commits/{sha}")["sha"])
        except NotFound:
            raise ToolError(f"commit '{sha}' not found in {repo}") from None

    def commit_files(self, repo: str, branch: str, message: str, files: dict[str, str]) -> str:
        self._gate(repo)
        for path in files:  # edits only: a path that does not exist is refused, not created
            try:
                self.file(repo, path, branch)
            except NotFound:
                raise ToolError(
                    f"file '{path}' does not exist on '{branch}'; new files are not allowed"
                ) from None
        try:
            head = self._request("GET", f"/repos/{repo}/git/ref/heads/{self._p(branch)}")
        except NotFound:
            raise ToolError(f"branch '{branch}' does not exist; call create_branch first") from None
        head_sha = str(head["object"]["sha"])
        base_tree = str(
            self._request("GET", f"/repos/{repo}/git/commits/{head_sha}")["tree"]["sha"]
        )
        tree = self._request(
            "POST",
            f"/repos/{repo}/git/trees",
            body={
                "base_tree": base_tree,
                "tree": [
                    {"path": p, "mode": "100644", "type": "blob", "content": c}
                    for p, c in sorted(files.items())
                ],
            },
        )
        commit = self._request(
            "POST",
            f"/repos/{repo}/git/commits",
            body={"message": message, "tree": tree["sha"], "parents": [head_sha]},
        )
        self._request(
            "PATCH",
            f"/repos/{repo}/git/refs/heads/{self._p(branch)}",
            body={"sha": commit["sha"], "force": False},
        )
        return str(commit["sha"])

    def open_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> tuple[int, str]:
        self._gate(repo)
        owner = repo.split("/", 1)[0]
        existing = self._request(
            "GET",
            f"/repos/{repo}/pulls",
            params={"head": f"{owner}:{head}", "base": base, "state": "open"},
        )
        if existing:  # idempotent: one open PR per head branch
            return int(existing[0]["number"]), str(existing[0]["html_url"])
        try:
            cmp = self._request("GET", f"/repos/{repo}/compare/{base}...{head}")
        except NotFound:
            raise ToolError(
                f"head branch '{head}' does not exist; call create_branch first"
            ) from None
        if int(cmp.get("ahead_by", 0)) < 1:
            raise ToolError(
                f"branch '{head}' has no changes; call commit_files first "
                "(an empty pull request is not allowed)"
            )
        pr = self._request(
            "POST",
            f"/repos/{repo}/pulls",
            body={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": True,
                "maintainer_can_modify": False,
            },
        )
        if not pr.get("draft"):  # never leave a ready-for-review PR behind
            raise ToolError("GitHub did not create a draft pull request; refusing to continue")
        return int(pr["number"]), str(pr["html_url"])


def _commit(c: dict[str, Any], files: list[RawFile]) -> RawCommit:
    meta = c.get("commit", {})
    author = meta.get("author") or {}
    return RawCommit(
        sha=str(c["sha"]),
        author=str((c.get("author") or {}).get("login") or author.get("name") or "unknown"),
        date=str(author.get("date", "")),
        message=str(meta.get("message", "")),
        files=files,
    )
