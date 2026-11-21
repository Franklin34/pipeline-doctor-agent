"""Fix a failure: reproduce it, let the model propose edits (at most 3 attempts), test every
proposal in the sandbox, and open a DRAFT pull request only if the tests really pass.

The model never touches anything. It returns structured edits; this code checks them against
``patch_policy``, applies them to a throw-away copy, runs the tests in the sandbox, and judges the
outcome. Every stage can end the attempt, and every ending is a ``FixOutcome`` the ticket reports.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from mcp.client import Client
from pd_sandbox import DockerSandbox, SandboxError, SandboxResult, judge

from pipeline_doctor_agent.audit import AuditLog
from pipeline_doctor_agent.fix.edits import EditError, apply_edits, unified_diff
from pipeline_doctor_agent.fix.models import FixOutcome
from pipeline_doctor_agent.fix.patch_policy import MAX_FILE_CHARS, check_edits
from pipeline_doctor_agent.fix.prompt import (
    AttemptNote,
    build_message,
    new_boundary,
    select_context,
)
from pipeline_doctor_agent.fix.proposer import FixProposalError, Proposer
from pipeline_doctor_agent.fix.publisher import PrPublisher, md, render_pr
from pipeline_doctor_agent.fix.source import RepoSource, SourceUnavailable
from pipeline_doctor_agent.guards import RunGuard
from pipeline_doctor_agent.investigator import InvestigationResult
from pipeline_doctor_agent.limits import RunBudget
from pipeline_doctor_agent.mcp_calls import ToolCallError
from pipeline_doctor_agent.policy.tools import RunScope, ToolPolicy
from pipeline_doctor_agent.settings import Settings

log = logging.getLogger("pipeline_doctor.fix")


class Sandbox(Protocol):
    image: str

    async def prepare(self, requirements: str | None) -> Path: ...
    async def run_tests(self, workspace: Path, deps: Path) -> SandboxResult: ...


ProposerFactory = Callable[[RunGuard], Proposer]


class FixService:
    def __init__(
        self,
        settings: Settings,
        *,
        source: RepoSource,
        sandbox: Sandbox | DockerSandbox,
        proposer_factory: ProposerFactory,
        github: Callable[[], Client],
    ) -> None:
        self.settings, self.source, self.sandbox = settings, source, sandbox
        self.proposer_factory = proposer_factory
        self.publisher = PrPublisher(github)

    async def attempt(self, result: InvestigationResult) -> FixOutcome:
        """Never raises for an expected failure: the outcome says what happened and why."""
        try:
            async with asyncio.timeout(self.settings.fix_timeout_s):
                return await self._attempt(result)
        except TimeoutError:
            return FixOutcome(
                status="error",
                reason=f"the fix attempt exceeded {self.settings.fix_timeout_s:.0f}s",
            )
        except SourceUnavailable as exc:
            return FixOutcome(status="skipped", reason=str(exc))
        except SandboxError as exc:
            log.error("sandbox failed", exc_info=True)
            return FixOutcome(status="error", reason=md(f"the sandbox failed: {exc}", 700))
        except ToolCallError as exc:
            log.error("publishing failed", exc_info=True)
            return FixOutcome(
                status="error", reason=md(f"opening the pull request failed: {exc}", 700)
            )

    async def _attempt(self, result: InvestigationResult) -> FixOutcome:
        s, ev = self.settings, result.event
        if not s.fix_enabled:
            return FixOutcome(
                status="skipped", reason="automatic fixes are disabled (FIX_ENABLED is not true)"
            )
        if not result.decision.allowed:
            return FixOutcome(
                status="skipped", reason="the fix gate did not allow an automatic fix"
            )
        repo = ev.repo or s.github_repo
        if repo is None:
            return FixOutcome(status="skipped", reason="the event names no repository")
        base_sha = ev.commit_sha or (
            result.code_findings.candidate_commits[-1].sha
            if result.code_findings.candidate_commits
            else None
        )
        if base_sha is None:
            return FixOutcome(status="skipped", reason="the failing commit is unknown")

        with tempfile.TemporaryDirectory(prefix="pd-fix-") as tmp:
            root = Path(tmp)
            base = root / "base"
            base.mkdir()
            await self.source.checkout(repo, base_sha, base)
            read = _reader(base)

            # 1. Reproduce. If the tests pass without any change the failure was flaky or environmental.
            deps = await self.sandbox.prepare(read("requirements.txt"))
            baseline = await self.sandbox.run_tests(base, deps)
            if not baseline.reproduces_failure:
                return FixOutcome(
                    status="skipped",
                    reason=_not_reproduced(baseline),
                )
            expected = {f"{t.class_name}::{t.name}" for t in result.processed.tests.failed}
            if expected and not expected & set(baseline.tests.failed_tests):
                return FixOutcome(
                    status="skipped",
                    reason="the tests that fail in the sandbox are not the ones that failed in the pipeline; "
                    "the failure depends on something the sandbox does not have",
                )

            # 2. Propose, check, apply, test, judge: at most `fix_max_attempts` times.
            budget = RunBudget(
                max_llm_calls=s.fix_max_attempts, max_tool_calls=0, token_budget=s.token_budget
            )
            guard = RunGuard(
                policy=ToolPolicy(RunScope(ev.job_name, repo)),
                audit=AuditLog(ev.idempotency_key),
                budget=budget,
            )
            proposer = self.proposer_factory(guard)
            context = select_context(read, _context_paths(result, baseline))
            history: list[AttemptNote] = []

            for n in range(1, s.fix_max_attempts + 1):
                message = build_message(
                    new_boundary(), result, baseline, context, history, n, s.fix_max_attempts
                )
                try:
                    proposal = await proposer.propose(message)
                except FixProposalError as exc:
                    history.append(AttemptNote(n, "(no valid proposal)", str(exc)))
                    if "run limit" in str(exc):
                        break
                    continue

                problems = check_edits(proposal.edits, result.diagnosis.category, read)
                if problems:
                    history.append(
                        AttemptNote(
                            n,
                            proposal.explanation,
                            "rejected by the patch rules: " + "; ".join(problems),
                        )
                    )
                    continue
                originals = {
                    e.path: text for e in proposal.edits if (text := read(e.path)) is not None
                }
                try:
                    updated = apply_edits(originals, proposal.edits)
                except EditError as exc:
                    history.append(
                        AttemptNote(
                            n, proposal.explanation, f"the edits could not be applied: {exc}"
                        )
                    )
                    continue
                if not updated:
                    history.append(
                        AttemptNote(n, proposal.explanation, "the edits changed nothing")
                    )
                    continue

                patched = root / f"attempt{n}"
                shutil.copytree(base, patched, symlinks=False)
                for path, content in updated.items():
                    _write_inside(patched, path, content)
                new_deps = deps
                if any(_is_requirements(p) for p in updated):
                    new_deps = await self.sandbox.prepare(read_text(patched / "requirements.txt"))
                after = await self.sandbox.run_tests(patched, new_deps)
                verdict = judge(baseline, after)
                if verdict.ok:
                    diff = unified_diff(originals, updated)
                    title, body = render_pr(
                        result,
                        proposal.explanation,
                        diff,
                        baseline,
                        after,
                        n,
                        s.fix_max_attempts,
                        self.sandbox.image,
                    )
                    url, branch = await self.publisher.publish(
                        result,
                        repo,
                        base_sha,
                        updated,
                        title,
                        body,
                        f"Fix {result.diagnosis.category.value} failure in {md(ev.job_name, 80)} (Pipeline Doctor)",
                    )
                    return FixOutcome(
                        status="pr_opened",
                        pr_url=url,
                        branch=branch,
                        attempts=n,
                        verification=f"{baseline.tests.broken} of {baseline.tests.total} tests failed before; all {after.tests.total} pass after, in an isolated sandbox",
                    )
                history.append(
                    AttemptNote(
                        n,
                        proposal.explanation,
                        "the tests did not pass: "
                        + "; ".join(verdict.reasons)
                        + "\n"
                        + after.output_excerpt[-1500:],
                    )
                )

            return FixOutcome(
                status="failed",
                attempts=len(history),
                reason=md(
                    f"no passing fix after {len(history)} attempt(s); last result: {history[-1].outcome if history else 'none'}",
                    700,
                ),
            )


# ---- helpers ---------------------------------------------------------------------------------------


def _not_reproduced(baseline: SandboxResult) -> str:
    if baseline.timed_out:
        return "the test run timed out in the sandbox, so the failure could not be reproduced"
    if baseline.passed:
        return f"the failure was not reproduced: all {baseline.tests.total} tests pass in the sandbox (flaky or environment-specific)"
    return (
        "the sandbox run did not produce a failing test to fix (it crashed or collected no tests)"
    )


def _context_paths(result: InvestigationResult, baseline: SandboxResult) -> list[str]:
    """Files worth showing: those of the failing tests, then the ones the investigator pointed at."""
    paths: list[str] = []
    for name in baseline.tests.failed_tests:
        module = name.split("::", 1)[0]
        paths.append(module.replace(".", "/") + ".py")
    paths += result.code_findings.files_of_interest
    return list(dict.fromkeys(paths))


def _is_requirements(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name.startswith("requirements") and name.endswith(".txt")


def read_text(path: Path) -> str | None:
    try:
        return (
            path.read_text(encoding="utf-8") if path.is_file() and not path.is_symlink() else None
        )
    except (OSError, UnicodeDecodeError):
        return None


def _reader(root: Path) -> Callable[[str], str | None]:
    """Read a repository file by relative path; refuses anything that resolves outside the root."""
    resolved = root.resolve()

    def read(path: str) -> str | None:
        target = (resolved / path).resolve()
        if resolved != target and resolved not in target.parents:
            return None
        text = read_text(target)
        return text if text is not None and len(text) <= MAX_FILE_CHARS * 2 else None

    return read


def _write_inside(root: Path, path: str, content: str) -> None:
    target = (root / path).resolve()
    if root.resolve() not in target.parents:
        raise ToolCallError(f"refusing to write outside the workspace: {path}")
    target.write_text(content, encoding="utf-8")
