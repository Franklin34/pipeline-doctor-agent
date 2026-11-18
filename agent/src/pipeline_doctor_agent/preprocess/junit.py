"""JUnit XML parsing, hardened against hostile XML (entity expansion, DTDs)."""

from __future__ import annotations

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from pipeline_doctor_agent.preprocess.models import FailedTest, TestReport

MAX_XML_BYTES = 5_000_000
MAX_FAILED = 50
MAX_MESSAGE = 500


class JUnitError(ValueError):
    """The report could not be parsed safely."""


def parse_junit(xml: str) -> TestReport:
    if len(xml.encode()) > MAX_XML_BYTES:
        raise JUnitError("JUnit report too large")
    try:
        root = SafeET.fromstring(xml)
    except (SafeET.ParseError, DefusedXmlException) as exc:
        raise JUnitError(f"unparseable or unsafe JUnit XML: {type(exc).__name__}") from exc

    report = TestReport()
    for case in root.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            if len(report.failed) < MAX_FAILED:
                msg = problem.get("message") or (problem.text or "").strip().splitlines()[0:1]
                text = msg if isinstance(msg, str) else (msg[0] if msg else "")
                report.failed.append(
                    FailedTest(
                        class_name=case.get("classname", ""),
                        name=case.get("name", ""),
                        message=text[:MAX_MESSAGE],
                    )
                )
        elif case.find("skipped") is not None:
            report.skipped += 1
        else:
            report.passed += 1
    return report
