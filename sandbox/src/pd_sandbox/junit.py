"""JUnit XML summary (pytest ``--junitxml``). The file is written by code we do not trust."""

from __future__ import annotations

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from pd_sandbox.models import TestSummary

MAX_XML_BYTES = 5_000_000
MAX_FAILED = 100


def summarize_junit(xml: bytes) -> TestSummary:
    """Count outcomes. Anything unparseable or unsafe yields an empty summary (which never passes)."""
    if len(xml) > MAX_XML_BYTES:
        return TestSummary()
    try:
        root = SafeET.fromstring(xml)
    except (SafeET.ParseError, DefusedXmlException, ValueError):
        return TestSummary()
    summary = TestSummary()
    for case in root.iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("failure") is not None:
            summary.failed += 1
            if len(summary.failed_tests) < MAX_FAILED:
                summary.failed_tests.append(name)
        elif case.find("error") is not None:
            summary.errors += 1
            if len(summary.failed_tests) < MAX_FAILED:
                summary.failed_tests.append(name)
        elif case.find("skipped") is not None:
            summary.skipped += 1
        else:
            summary.passed += 1
    summary.failed_tests.sort()
    return summary
