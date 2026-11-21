"""Did the patch really fix it? Pure decision logic over two sandbox results."""

from __future__ import annotations

from dataclasses import dataclass

from pd_sandbox.models import SandboxResult


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: tuple[str, ...]


def judge(baseline: SandboxResult, after: SandboxResult) -> Verdict:
    """A fix counts only if ALL hold: the tests ran and exited cleanly, nothing fails, no test
    disappeared (deleting tests is not a fix), and every test that failed before now passes."""
    reasons: list[str] = []
    if after.timed_out:
        reasons.append("the test run timed out")
    if after.exit_code != 0 and not after.timed_out:
        reasons.append(f"the test run exited with code {after.exit_code}")
    if after.tests.total == 0:
        reasons.append("no tests ran (zero tests is never a pass)")
    if after.tests.broken:
        reasons.append(
            f"{after.tests.broken} tests still fail: {', '.join(after.tests.failed_tests[:5])}"
        )
    if after.tests.total < baseline.tests.total:
        reasons.append(
            f"fewer tests ran after the change ({after.tests.total}) than before ({baseline.tests.total})"
        )
    still = sorted(set(baseline.tests.failed_tests) & set(after.tests.failed_tests))
    if still and not after.tests.broken:  # already covered above otherwise
        reasons.append(f"previously failing tests still fail: {', '.join(still[:5])}")
    return Verdict(ok=not reasons, reasons=tuple(reasons))
