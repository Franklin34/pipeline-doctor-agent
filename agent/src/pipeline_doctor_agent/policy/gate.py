"""The fix gate: plain code decides whether a diagnosis may reach the fixer.

The LLM proposes a diagnosis; this function decides. It is deliberately boring: pure,
deterministic, no I/O, fully table-tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pipeline_doctor_agent.schemas import Category, CodeFindings, Diagnosis

FIXABLE_CATEGORIES = frozenset({Category.CODE, Category.TEST, Category.DEPENDENCY})


class Action(StrEnum):
    FIX = "fix"  # hand to the fix agent (sandbox, then draft PR)
    TICKET_ONLY = "ticket_only"  # diagnose and write it up; never change code


@dataclass(frozen=True)
class GateDecision:
    action: Action
    reasons: tuple[str, ...]  # why (for the ticket, audit log and metrics)

    @property
    def allowed(self) -> bool:
        return self.action is Action.FIX


def suspect_in_range(diagnosis: Diagnosis, code: CodeFindings | None) -> bool:
    """The suspect commit must be one the code investigator actually listed (prefix match either way)."""
    if diagnosis.suspect_commit is None or code is None:
        return False
    known = {c.sha.lower() for c in code.candidate_commits}
    sus = diagnosis.suspect_commit
    return any(sus.startswith(k) or k.startswith(sus) for k in known)


def decide(
    diagnosis: Diagnosis,
    code: CodeFindings | None,
    *,
    threshold: float,
    injection_flags: tuple[str, ...] = (),
) -> GateDecision:
    """Return FIX only if every condition holds; otherwise TICKET_ONLY with all failing reasons."""
    blockers: list[str] = []

    if diagnosis.category not in FIXABLE_CATEGORIES:
        blockers.append(f"category '{diagnosis.category}' is never auto-fixed")
    if not diagnosis.fixable:
        blockers.append("diagnosis says the failure is not fixable by a code change")
    if diagnosis.confidence < threshold:
        blockers.append(
            f"confidence {diagnosis.confidence:.2f} is below the threshold {threshold:.2f}"
        )
    if diagnosis.suspect_commit is None:
        blockers.append("no suspect commit identified")
    elif not suspect_in_range(diagnosis, code):
        # Guards against a hallucinated or injected SHA: it must come from the commit range
        # the code investigator actually listed.
        blockers.append(
            "suspect commit is not among the commits found between last green and failing build"
        )
    for flag in injection_flags:
        blockers.append(f"flagged by input screening: {flag}")

    if blockers:
        return GateDecision(Action.TICKET_ONLY, tuple(blockers))
    return GateDecision(Action.FIX, ("all gate conditions satisfied",))
