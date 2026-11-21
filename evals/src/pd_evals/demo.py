"""A narrated run of the real pipeline, offline: ``make demo``.

Everything here is the real system except the language model, which is the scripted one from the
golden dataset (no keys, no network, no cost). The Jenkins MCP server is the real Go binary, the
GitHub and Jira servers are the real MCP servers over in-memory fixtures, and the scrubber, policy,
screening, gate and ticket renderer all run for real.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

from mcp.client import Client
from pd_mcp_github.backend import MockBackend as GitHubMock
from pd_mcp_github.server import build_server as github_server
from pd_mcp_jira.backend import MockBackend as JiraMock
from pd_mcp_jira.server import build_server as jira_server
from pipeline_doctor_agent.fake_llm import ScriptedLlm
from pipeline_doctor_agent.investigator import InvestigationError, Investigator
from pipeline_doctor_agent.schemas import PipelineFailedEvent
from pipeline_doctor_agent.settings import Settings
from pipeline_doctor_agent.ticket import TicketService

from pd_evals.runner import build_jenkins_mcp
from pd_evals.scenario import Scenario, load_scenarios

ROOT = Path(__file__).resolve().parents[3]
DEFAULT = ("logic-bug", "obedient-to-injection", "hallucinated-suspect")


def say(title: str, *lines: str) -> None:
    print(f"\n\033[1m{title}\033[0m")
    for line in lines:
        print(textwrap.indent(line, "  "))


async def narrate(sc: Scenario, settings: Settings) -> None:
    print("\n" + "=" * 78)
    print(f"\033[1;36mScenario: {sc.id}\033[0m  {sc.description}")
    print("=" * 78)
    jira, github = JiraMock.load(ROOT / "fixtures"), GitHubMock.load(ROOT / "fixtures")
    llm = ScriptedLlm(scripts=copy.deepcopy(sc.scripted_model))
    investigator = Investigator(settings, small_model=llm, large_model=llm)
    tickets = TicketService(
        settings,
        jira=lambda: Client(jira_server(jira)),
        github=lambda: Client(github_server(github)),
    )
    event = PipelineFailedEvent(**sc.event)
    say("1. A Jenkins build failed", f"{event.job_name} #{event.build_number} (repo {event.repo})")
    try:
        r = await investigator.investigate(event)
    except InvestigationError as exc:
        say("Investigation failed", f"{exc.kind}: {exc}")
        return
    p = r.processed
    say(
        "2. Facts, with no model involved",
        f"error type:   {p.error_type}",
        f"error:        {p.error_message}",
        f"fingerprint:  {p.fingerprint}   (same failure => same fingerprint => one ticket)",
        f"secrets removed before any model sees the log: {dict(p.redactions) or 'none found'}",
    )
    say(
        "3. What the agents did (every tool call is checked by policy and audited)",
        *[f"{rec.agent:18} {rec.tool:26} {rec.status.upper()}" for rec in r.audit],
    )
    refused = [rec for rec in r.audit if rec.status == "blocked"]
    if refused:
        say(
            "   The model tried things it may not do. Code refused them:",
            *[f"{rec.agent} -> {rec.tool}: {rec.detail}" for rec in refused],
        )
    d = r.diagnosis
    say(
        "4. Diagnosis (what the model proposed)",
        f"category:    {d.category}   confidence: {d.confidence:.2f}   fixable: {d.fixable}",
        f"root cause:  {d.root_cause}",
        f"suspect:     {d.suspect_commit or '(none)'}",
    )
    if r.injection_flags:
        say("5. Input screening flagged instructions hidden in the build data", *r.injection_flags)
    say(
        "6. The gate (plain code, not the model) decides what may happen next",
        f"decision: {str(r.decision.action).upper()}",
        *[f"- {reason}" for reason in r.decision.reasons],
    )
    outcome = await tickets.record(r)
    issue = jira.issues[outcome.issue_key]
    body = (issue.description or (issue.comments[-1] if issue.comments else "")).splitlines()
    say(
        f"7. Jira: ticket {outcome.issue_key} {outcome.action}",
        f"assignee: {outcome.assignee or '(unassigned)'}   labels: {', '.join(issue.labels)}",
        "--- first lines of the ticket ---",
        *body[:14],
        "...",
    )
    say(
        "8. What did NOT happen",
        "No code was changed, nothing was merged or pushed, no build was re-run."
        if str(r.decision.action) != "fix"
        else "A fix may be attempted next in an isolated sandbox, ending as a DRAFT pull request for a human.",
        f"GitHub writes made: {len(github.pull_requests)} pull requests, {len(github.branches)} branches.",
    )


async def amain(args: argparse.Namespace) -> None:
    scenarios = load_scenarios(ROOT / "fixtures", set(args.scenario or DEFAULT))
    settings = replace(
        Settings(),
        fixtures_dir=str(ROOT / "fixtures"),
        jenkins_mcp_command=os.environ.get("JENKINS_MCP_COMMAND") or build_jenkins_mcp(ROOT),
        run_timeout_s=120,
    )
    for sc in scenarios:
        await narrate(sc, settings)


def main() -> None:
    p = argparse.ArgumentParser(prog="pd_evals.demo", description=__doc__)
    p.add_argument("scenario", nargs="*", help=f"scenario ids (default: {', '.join(DEFAULT)})")
    asyncio.run(amain(p.parse_args()))
    sys.exit(0)


if __name__ == "__main__":
    main()
