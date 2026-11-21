"""Jira Cloud REST backend (real mode), one project only.

* **One project.** The project key is configuration. Every search is constrained to it and every
  issue is created in it; no tool argument can name another project.
* **Fingerprint as a label.** A failure's fingerprint is stored as the label ``pd-fp-<fingerprint>``:
  searchable, needs no custom field, and visible to humans.
* **ADF, built structurally** (see ``adf.py``): text from logs cannot become markup.
* **Assignment by mapping.** CODEOWNERS handles map to Jira account ids through a reviewed JSON file.
  An unmapped owner leaves the issue unassigned (they are listed in the ticket body anyway), and so
  does a rejected assignment: a ticket without an owner beats no ticket.
* Secrets stay out of messages; reads retry on transient errors, writes never do.

Credentials: an Atlassian API token for a dedicated service account, with access to this one
project only (``JIRA_EMAIL`` and ``JIRA_API_TOKEN``, HTTP Basic).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from pd_mcp_jira.adf import adf_text, to_adf
from pd_mcp_jira.backend import CLOSED, Issue

TIMEOUT_S = 15.0
MAX_BODY = 5_000_000
RETRY_STATUS = {502, 503, 504}
LABEL_PREFIX = "pd-fp-"
MAX_COMMENT_SCAN = 100
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PROJECT = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
_LOCAL = {"localhost", "127.0.0.1"}


def _short(value: object, limit: int = 200) -> str:
    return _CONTROL.sub(" ", str(value))[:limit]


def load_user_map(path: str | None) -> dict[str, str]:
    """``{"dev1": "<jira account id>"}``. Handles are matched case-insensitively, without a leading @."""
    if not path:
        return {}
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise ValueError("the Jira user map must be a JSON object of handle -> account id")
    return {k.lower().lstrip("@"): v for k, v in raw.items()}


class JiraRestBackend:
    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        project: str,
        *,
        issue_type: str = "Task",
        user_map: dict[str, str] | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not (email.strip() and api_token.strip()):
            raise ValueError("Jira credentials are required (JIRA_EMAIL and JIRA_API_TOKEN)")
        if not _PROJECT.match(project):
            raise ValueError("JIRA_PROJECT_KEY must look like DEMO")
        url = urlparse(base_url)
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in _LOCAL):
            raise ValueError("JIRA_BASE_URL must use https")
        self.base = base_url.rstrip("/")
        self.project, self.issue_type = project, issue_type
        self.user_map = {k.lower().lstrip("@"): v for k, v in (user_map or {}).items()}
        self._auth = (email, api_token)
        self._client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
        self._sleep = sleep

    # -- plumbing ---------------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        read: bool = False,
    ) -> Any:
        """``read`` marks a POST that changes nothing (Jira's search): it may retry like a GET."""
        attempts = 3 if method == "GET" or read else 1
        for attempt in range(attempts):
            try:
                resp = self._client.request(
                    method,
                    self.base + path,
                    params=params,
                    json=body,
                    auth=self._auth,
                    headers={"Accept": "application/json", "User-Agent": "pipeline-doctor"},
                    timeout=TIMEOUT_S,
                )
            except httpx.TimeoutException:
                raise ToolError("Jira did not answer in time; try again later") from None
            except httpx.HTTPError as exc:
                raise ToolError(f"could not reach Jira ({type(exc).__name__})") from None
            if resp.status_code in RETRY_STATUS and attempt + 1 < attempts:
                self._sleep(0.5 * (attempt + 1))
                continue
            return self._decode(resp)
        raise AssertionError("unreachable")  # pragma: no cover

    def _decode(self, resp: httpx.Response) -> Any:
        if len(resp.content) > MAX_BODY:
            raise ToolError("Jira's response was too large to handle")
        if resp.status_code >= 400:
            raise self._error(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise ToolError("Jira returned something that is not JSON") from None

    @staticmethod
    def _error(resp: httpx.Response) -> ToolError:
        code = resp.status_code
        detail = ""
        try:
            data = resp.json()
            detail = _short(data.get("errors") or data.get("errorMessages") or "")
        except (ValueError, AttributeError):
            pass
        if code == 401:
            return ToolError(
                "Jira rejected the credentials (email or API token wrong, expired or revoked)"
            )
        if code == 403:
            return ToolError(
                "Jira refused the request: the account lacks permission for this project"
            )
        if code == 404:
            return ToolError(
                "not found, or the account cannot see it; check the project and issue key"
            )
        if code == 429:
            return ToolError(
                f"Jira rate limit reached (retry after {_short(resp.headers.get('retry-after', '?'), 20)})"
            )
        if code == 400:
            return ToolError(f"Jira rejected the request as invalid: {detail}")
        return ToolError(f"Jira error {code}")

    def _url(self, key: str) -> str:
        return f"{self.base}/browse/{key}"

    # -- the backend contract --------------------------------------------------------------------
    def search_by_fingerprint(self, fingerprint: str) -> list[Issue]:
        if not re.fullmatch(r"[a-f0-9]{16,64}", fingerprint):  # also keeps JQL injection impossible
            raise ToolError("invalid fingerprint")
        jql = f'project = "{self.project}" AND labels = "{LABEL_PREFIX}{fingerprint}" ORDER BY created DESC'
        data = self._request(
            "POST",
            "/rest/api/3/search/jql",
            body={"jql": jql, "fields": ["summary", "status", "labels"], "maxResults": 20},
            read=True,
        )
        out: list[Issue] = []
        for i in data.get("issues", []):
            f = i.get("fields", {})
            status = f.get("status", {})
            done = status.get("statusCategory", {}).get("key") == "done"
            name = (
                "Done"
                if done and str(status.get("name", "")).lower() not in CLOSED
                else str(status.get("name", ""))
            )
            out.append(
                Issue(
                    key=i["key"],
                    summary=str(f.get("summary", "")),
                    status=name,
                    fingerprint=fingerprint,
                    url=self._url(i["key"]),
                    labels=[str(x) for x in f.get("labels", [])],
                )
            )
        return out

    def create_issue(self, issue: Issue) -> Issue:
        labels = sorted({*issue.labels, f"{LABEL_PREFIX}{issue.fingerprint}"})
        fields: dict[str, Any] = {
            "project": {"key": self.project},
            "issuetype": {"name": self.issue_type},
            "summary": issue.summary[:255],
            "description": to_adf(issue.description),
            "labels": labels,
        }
        account = self.user_map.get((issue.assignee or "").lower().lstrip("@"))
        if account:
            fields["assignee"] = {"accountId": account}
        try:
            created = self._request("POST", "/rest/api/3/issue", body={"fields": fields})
        except ToolError as exc:
            if (
                account and "assignee" in str(exc).lower()
            ):  # an unassignable owner must not lose the ticket
                del fields["assignee"]
                account = None  # the ticket exists without an owner; report that honestly
                created = self._request("POST", "/rest/api/3/issue", body={"fields": fields})
            else:
                raise
        key = str(created["key"])
        return issue.model_copy(
            update={
                "key": key,
                "status": "Open",
                "url": self._url(key),
                "assignee": issue.assignee if account else None,
                "labels": labels,
            }
        )

    def add_comment(self, key: str, body: str, marker: str | None = None) -> bool:
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}-[0-9]{1,7}", key) or not key.startswith(
            self.project + "-"
        ):
            raise ToolError(f"issue '{key}' is not in project {self.project}")
        if marker:
            data = self._request(
                "GET",
                f"/rest/api/3/issue/{key}/comment",
                params={"maxResults": str(MAX_COMMENT_SCAN), "orderBy": "-created"},
            )
            if any(marker in adf_text(c.get("body")) for c in data.get("comments", [])):
                return False
        self._request("POST", f"/rest/api/3/issue/{key}/comment", body={"body": to_adf(body)})
        return True
