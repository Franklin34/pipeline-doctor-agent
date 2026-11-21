"""The ticket text the agent really renders must convert to valid ADF with nothing lost."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pd_mcp_jira.adf import adf_text, to_adf
from pipeline_doctor_agent.fix.models import FixOutcome
from pipeline_doctor_agent.settings import Settings
from pipeline_doctor_agent.ticket import render_comment, render_issue


def kinds(doc: dict[str, Any]) -> list[str]:
    return [n["type"] for n in doc["content"]]


@pytest.mark.parametrize("category", ["code", "infrastructure", "flaky", "configuration"])
def test_every_kind_of_ticket_converts_to_valid_adf(make_result: Any, category: str) -> None:
    result = make_result(category=category, fixable=category == "code")
    _summary, body = render_issue(
        Settings(), result, repo="acme/demo-service", pr_url=None, owners=["@dev1"], previous=[]
    )
    doc = to_adf(body)
    assert doc["type"] == "doc" and {"heading", "bulletList", "codeBlock", "rule"} <= set(
        kinds(doc)
    )
    text = adf_text(doc)
    for must in (
        result.diagnosis.root_cause[:40],
        result.fingerprint,
        "Pipeline Doctor",
        "demo-service/main",
    ):
        assert must in text
    assert "{noformat}" not in text and "h3." not in text  # no raw markup left behind
    json.dumps(doc)


def test_links_to_the_build_the_commit_and_the_pull_request_survive(make_result: Any) -> None:
    result = make_result()
    fix = FixOutcome(
        status="pr_opened",
        pr_url="https://github.com/acme/demo-service/pull/7",
        verification="2 of 3 tests failed before",
    )
    _s, body = render_issue(
        Settings(),
        result,
        repo="acme/demo-service",
        pr_url=fix.pr_url,
        owners=[],
        previous=[],
        fix=fix,
    )
    doc = to_adf(body)
    hrefs: list[str] = []
    stack: list[Any] = [doc]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            hrefs += [m["attrs"]["href"] for m in n.get("marks", [])]
            stack += n.get("content", [])
    assert "https://jenkins.example/job/demo-service/main/12/" in hrefs
    assert any("/commit/" in h for h in hrefs)
    assert "https://github.com/acme/demo-service/pull/7" in adf_text(doc)


def test_a_hostile_log_line_in_the_rendered_ticket_stays_inert(make_result: Any) -> None:
    evil = "E  {code} [~accountid:1] [x|javascript:alert(1)] !pwn.png! <script>x</script>"
    result = make_result(error_lines=[evil, "FAILED tests/test_a.py::test_a"])
    _s, body = render_issue(
        Settings(), result, repo="acme/demo-service", pr_url=None, owners=[], previous=[]
    )
    doc = to_adf(body)
    sent = json.dumps(doc)
    assert "mention" not in sent
    assert '"href": "javascript' not in sent and "media" not in sent


def test_the_comment_with_its_idempotency_marker_converts(make_result: Any) -> None:
    result = make_result()
    body = render_comment(Settings(), result, repo="acme/demo-service", pr_url=None)
    doc = to_adf(f"{body}\n\n[pd-occurrence:demo-service/main:12]")
    assert "[pd-occurrence:demo-service/main:12]" in adf_text(doc)
