from __future__ import annotations

import pytest
from pipeline_doctor_agent.preprocess.junit import JUnitError, parse_junit

GOOD = """<?xml version="1.0"?>
<testsuites><testsuite name="s" tests="4">
  <testcase classname="tests.a" name="test_ok"/>
  <testcase classname="tests.a" name="test_skip"><skipped/></testcase>
  <testcase classname="tests.a" name="test_bad"><failure message="assert 1 == 2">trace</failure></testcase>
  <testcase classname="tests.b" name="test_err"><error>ValueError: boom\nmore</error></testcase>
</testsuite></testsuites>"""


def test_parses_counts_and_failures() -> None:
    r = parse_junit(GOOD)
    assert (r.passed, r.skipped) == (1, 1)
    assert [(f.class_name, f.name, f.message) for f in r.failed] == [
        ("tests.a", "test_bad", "assert 1 == 2"),
        ("tests.b", "test_err", "ValueError: boom"),
    ]


def test_rejects_entity_expansion_bomb() -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]><testsuite><testcase name="&b;" classname="x"/></testsuite>'
    )
    with pytest.raises(JUnitError):
        parse_junit(bomb)


def test_rejects_external_entity() -> None:
    xxe = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        '<testsuite><testcase name="&x;" classname="c"/></testsuite>'
    )
    with pytest.raises(JUnitError):
        parse_junit(xxe)


def test_rejects_malformed_and_oversized() -> None:
    with pytest.raises(JUnitError):
        parse_junit("<testsuite>")
    with pytest.raises(JUnitError):
        parse_junit("<a>" + "x" * 6_000_000 + "</a>")


def test_failure_list_is_capped() -> None:
    cases = "".join(
        f'<testcase classname="c" name="t{i}"><failure message="m"/></testcase>' for i in range(80)
    )
    assert len(parse_junit(f"<testsuite>{cases}</testsuite>").failed) == 50
