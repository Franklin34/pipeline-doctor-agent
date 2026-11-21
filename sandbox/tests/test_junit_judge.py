from __future__ import annotations

from pd_sandbox import SandboxResult, TestSummary, judge
from pd_sandbox.junit import summarize_junit

XML = b"""<testsuites><testsuite>
 <testcase classname="tests.a" name="test_ok"/>
 <testcase classname="tests.a" name="test_skip"><skipped/></testcase>
 <testcase classname="tests.a" name="test_bad"><failure message="x"/></testcase>
 <testcase classname="tests.b" name="test_err"><error/></testcase>
</testsuite></testsuites>"""


def test_summary_counts() -> None:
    s = summarize_junit(XML)
    assert (s.passed, s.failed, s.errors, s.skipped, s.total, s.broken) == (1, 1, 1, 1, 4, 2)
    assert s.failed_tests == ["tests.a::test_bad", "tests.b::test_err"]


def test_hostile_or_broken_xml_yields_an_empty_summary_that_cannot_pass() -> None:
    bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><t><testcase name="&b;"/></t>'
    xxe = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]><t><testcase name="&x;"/></t>'
    for bad in (bomb, xxe, b"<broken", b"", b"x" * 6_000_000):
        assert summarize_junit(bad).total == 0


def result(
    exit_code: int | None = 0,
    passed: int = 3,
    failed: int = 0,
    names: list[str] | None = None,
    timed_out: bool = False,
) -> SandboxResult:
    return SandboxResult(
        exit_code=exit_code,
        timed_out=timed_out,
        duration_s=1.0,
        tests=TestSummary(passed=passed, failed=failed, failed_tests=names or []),
    )


def test_passed_property() -> None:
    assert result().passed
    assert not result(exit_code=1, passed=2, failed=1, names=["a::b"]).passed
    assert not result(passed=0).passed  # zero tests is never a pass
    assert not result(exit_code=None, timed_out=True).passed
    assert not result(exit_code=5, passed=0).passed


def test_reproduces_failure() -> None:
    assert result(exit_code=1, passed=2, failed=1, names=["a::b"]).reproduces_failure
    assert not result().reproduces_failure  # baseline passed: flaky or not reproducible
    assert not result(exit_code=None, timed_out=True, passed=0).reproduces_failure
    assert not result(
        exit_code=2, passed=0
    ).reproduces_failure  # crashed collection, no failing test to fix


BASE = result(exit_code=1, passed=2, failed=1, names=["m::t"])


def test_judge_accepts_a_real_fix() -> None:
    v = judge(BASE, result(passed=3))
    assert v.ok and v.reasons == ()


def test_judge_rejects_each_kind_of_cheating_or_failure() -> None:
    cases = {
        "still failing": result(exit_code=1, passed=2, failed=1, names=["m::t"]),
        "new failure": result(exit_code=1, passed=2, failed=1, names=["m::other"]),
        "tests deleted": result(
            passed=2
        ),  # exits 0 but one test fewer: deleting the failing test is not a fix
        "no tests": result(passed=0),
        "timeout": result(exit_code=None, passed=0, timed_out=True),
        "bad exit with green tests": result(exit_code=3, passed=3),
    }
    for label, after in cases.items():
        v = judge(BASE, after)
        assert not v.ok and v.reasons, label
    assert "fewer tests" in judge(BASE, cases["tests deleted"]).reasons[0]
