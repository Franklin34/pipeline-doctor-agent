from __future__ import annotations

import itertools

import pytest
from pipeline_doctor_agent.policy.gate import Action, decide
from pipeline_doctor_agent.schemas import Category, CodeFindings, CommitRef, Diagnosis

SHA = "c3d4e5f60718293a4b5c6d7e8f90123456789012"
CODE = CodeFindings(
    candidate_commits=[CommitRef(sha=SHA, reason="changes the formula")], suspect_commit=SHA
)


def diag(**kw: object) -> Diagnosis:
    base: dict[str, object] = {
        "category": Category.CODE,
        "root_cause": "formula inverted in apply_discount",
        "evidence": ["assert 10 == 90"],
        "suspect_commit": SHA,
        "confidence": 0.9,
        "fixable": True,
    }
    return Diagnosis.model_validate(base | kw)


def test_all_conditions_met_allows_fix() -> None:
    d = decide(diag(), CODE, threshold=0.8)
    assert d.action is Action.FIX and d.allowed


@pytest.mark.parametrize("category", [Category.CODE, Category.TEST, Category.DEPENDENCY])
def test_fixable_categories(category: Category) -> None:
    assert decide(diag(category=category), CODE, threshold=0.8).allowed


@pytest.mark.parametrize(
    "category", [Category.INFRASTRUCTURE, Category.FLAKY, Category.CONFIGURATION]
)
def test_never_fixes_other_categories_even_when_confident(category: Category) -> None:
    d = decide(diag(category=category, confidence=1.0, fixable=True), CODE, threshold=0.0)
    assert d.action is Action.TICKET_ONLY and "never auto-fixed" in d.reasons[0]


@pytest.mark.parametrize(
    ("confidence", "threshold", "allowed"),
    [
        (0.79, 0.8, False),
        (0.8, 0.8, True),
        (0.81, 0.8, True),
        (0.0, 0.0, True),
        (1.0, 1.0, True),
        (0.99, 1.0, False),
    ],
)
def test_confidence_threshold_is_inclusive(
    confidence: float, threshold: float, allowed: bool
) -> None:
    assert decide(diag(confidence=confidence), CODE, threshold=threshold).allowed is allowed


def test_not_fixable_flag_blocks() -> None:
    d = decide(diag(fixable=False), CODE, threshold=0.8)
    assert not d.allowed and any("not fixable" in r for r in d.reasons)


def test_missing_suspect_blocks() -> None:
    d = decide(diag(suspect_commit=None), CODE, threshold=0.8)
    assert not d.allowed and any("no suspect commit" in r for r in d.reasons)


def test_suspect_must_come_from_the_investigated_range() -> None:
    other = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    assert not decide(diag(suspect_commit=other), CODE, threshold=0.8).allowed
    assert not decide(diag(), None, threshold=0.8).allowed  # no code findings at all
    assert not decide(diag(), CodeFindings(candidate_commits=[]), threshold=0.8).allowed


def test_short_sha_matches_full_candidate() -> None:
    assert decide(diag(suspect_commit=SHA[:7]), CODE, threshold=0.8).allowed


def test_injection_flags_block() -> None:
    d = decide(
        diag(), CODE, threshold=0.8, injection_flags=("commit message contains instructions",)
    )
    assert not d.allowed and any("input screening" in r for r in d.reasons)


def test_all_reasons_are_reported() -> None:
    d = decide(
        diag(category=Category.FLAKY, confidence=0.1, fixable=False, suspect_commit=None),
        None,
        threshold=0.8,
    )
    assert len(d.reasons) == 4


def test_exhaustive_only_the_all_good_combination_fixes() -> None:
    """Brute force the boolean conditions: exactly one combination may reach the fixer."""
    allowed = []
    for fixable_cat, fixable, confident, suspect_ok in itertools.product([True, False], repeat=4):
        d = decide(
            diag(
                category=Category.CODE if fixable_cat else Category.INFRASTRUCTURE,
                fixable=fixable,
                confidence=0.95 if confident else 0.1,
                suspect_commit=SHA if suspect_ok else None,
            ),
            CODE,
            threshold=0.8,
        )
        if d.allowed:
            allowed.append((fixable_cat, fixable, confident, suspect_ok))
    assert allowed == [(True, True, True, True)]
