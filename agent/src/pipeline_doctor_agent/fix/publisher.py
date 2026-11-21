"""Open the draft pull request: branch, commit, PR, all through the policy-checked git write tools."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AsyncExitStack

from mcp.client import Client
from pd_sandbox import SandboxResult

from pipeline_doctor_agent.audit import AuditLog
from pipeline_doctor_agent.investigator import InvestigationResult
from pipeline_doctor_agent.mcp_calls import PolicySession, ToolCallError, flatten_errors
from pipeline_doctor_agent.policy.tools import RunScope, ToolPolicy
from pipeline_doctor_agent.preprocess.scrub import scrub

AGENT = "pr_publisher"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def md(value: str, limit: int = 600) -> str:
    """One scrubbed, control-free, length-capped line for use in Markdown text."""
    value = re.sub(r"\s+", " ", _CONTROL.sub("", scrub(value).text)).strip()
    return value if len(value) <= limit else value[: limit - 3] + "..."


def render_pr(
    result: InvestigationResult,
    explanation: str,
    diff: str,
    baseline: SandboxResult,
    after: SandboxResult,
    attempts: int,
    max_attempts: int,
    image: str,
) -> tuple[str, str]:
    d, ev = result.diagnosis, result.event
    title = f"[Pipeline Doctor] Fix {d.category.value}: {md(d.root_cause, 120)}"
    safe_diff = scrub(diff).text.replace("~~~~", "~~~ ~")
    body = "\n".join(
        [
            "## Automated fix proposed by Pipeline Doctor (draft)",
            "",
            f"**Failure:** `{md(ev.job_name, 100)}` build #{ev.build_number}"
            + (f" ([build]({ev.build_url}))" if ev.build_url else ""),
            f"**Category:** {d.category.value}, confidence {d.confidence:.2f}",
            f"**Root cause:** {md(d.root_cause, 800)}",
            "",
            "### What changed",
            md(explanation, 600),
            "",
            "~~~~diff",
            safe_diff.rstrip(),
            "~~~~",
            "",
            "### Verification",
            f"- Reproduced before the change: {baseline.tests.broken} of {baseline.tests.total} tests failing "
            f"({', '.join(md(t, 80) for t in baseline.tests.failed_tests[:5])}).",
            f"- After the change: all {after.tests.total} tests pass.",
            f"- Ran in an isolated container (`{md(image, 80)}`): no network, read-only filesystem, no secrets, "
            "non-root, resource limits.",
            f"- Attempts used: {attempts} of {max_attempts}.",
            "",
            "### Evidence",
            *[f"- {md(e, 400)}" for e in d.evidence],
            "",
            f"Fingerprint: `{result.fingerprint}`",
            "",
            "**This is a draft. A human must review it, run the full pipeline, and decide whether to merge.**",
        ]
    )
    return title[:200], scrub(body).text[:19_000]


class PrPublisher:
    def __init__(self, github: Callable[[], Client]) -> None:
        self._github = github

    async def publish(
        self,
        result: InvestigationResult,
        repo: str,
        base_sha: str,
        files: dict[str, str],
        title: str,
        body: str,
        message: str,
    ) -> tuple[str, str]:
        """Return (pr_url, branch). Safe to repeat: an existing branch is reused and the PR tool
        returns the already-open pull request for the same branch."""
        ev = result.event
        branch = f"pipeline-doctor/fix-{result.fingerprint[:8]}-b{ev.build_number}"
        audit = AuditLog(run_id=ev.idempotency_key)
        policy = ToolPolicy(RunScope(job_name=ev.job_name, repo=repo))
        with flatten_errors():
            return await self._publish(
                result, repo, base_sha, files, title, body, message, branch, policy, audit
            )

    async def _publish(
        self,
        result: InvestigationResult,
        repo: str,
        base_sha: str,
        files: dict[str, str],
        title: str,
        body: str,
        message: str,
        branch: str,
        policy: ToolPolicy,
        audit: AuditLog,
    ) -> tuple[str, str]:
        async with AsyncExitStack() as stack:
            git = PolicySession(
                await stack.enter_async_context(self._github()), policy, audit, AGENT
            )
            default = (await git.call("get_repo_info", repo=repo))["default_branch"]
            try:
                await git.call("create_branch", repo=repo, name=branch, from_sha=base_sha)
            except ToolCallError as exc:
                if "already exists" not in str(exc):
                    raise  # a redelivery after a crash lands here and simply continues
            await git.call(
                "commit_files",
                repo=repo,
                branch=branch,
                message=message,
                files=[{"path": p, "content": c} for p, c in sorted(files.items())],
            )
            pr = await git.call(
                "open_draft_pull_request",
                repo=repo,
                head=branch,
                base=default,
                title=title,
                body=body,
            )
        return str(pr["url"]), branch
