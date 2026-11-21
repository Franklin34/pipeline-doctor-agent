"""Entry point: raw log (+ optional JUnit XML) -> scrubbed, structured facts."""

from __future__ import annotations

from collections import Counter

from pipeline_doctor_agent.preprocess.fingerprint import compute_fingerprint
from pipeline_doctor_agent.preprocess.junit import JUnitError, parse_junit
from pipeline_doctor_agent.preprocess.logparse import (
    extract_facts,
    infer_failed_stage,
    stage_lines,
)
from pipeline_doctor_agent.preprocess.models import FailedTest, ProcessedFailure, TestReport
from pipeline_doctor_agent.preprocess.scrub import scrub

# Hard cap before any regex runs: bounds CPU on hostile or runaway logs. Dropping text can
# only remove information, never leak it, so truncating before scrubbing is safe.
MAX_LOG_CHARS = 1_000_000
MAX_SCAN_LINE = 4_000  # longer lines are cut before pattern matching


def preprocess(
    log: str | list[str],
    junit_xml: str | None = None,
    failed_stage: str | None = None,
    test_report: TestReport | None = None,
) -> ProcessedFailure:
    """Scrub first, then extract. Nothing downstream ever sees unscrubbed text."""
    redactions: Counter[str] = Counter()
    warnings: list[str] = []

    raw = log if isinstance(log, str) else "\n".join(log)
    if len(raw) > MAX_LOG_CHARS:
        warnings.append(f"log truncated to the last {MAX_LOG_CHARS} characters")
        raw = raw[-MAX_LOG_CHARS:]  # failures are usually at the end
    scrubbed = scrub(raw)
    redactions.update(scrubbed.counts)
    all_lines = [line[:MAX_SCAN_LINE] for line in scrubbed.text.splitlines()]

    stage = failed_stage or infer_failed_stage(all_lines)
    scoped = stage_lines(all_lines, stage) if stage else None
    if stage and scoped is None:
        warnings.append(f"stage '{stage}' not found in log; analysed the whole log")
    facts = extract_facts(scoped if scoped is not None else all_lines)

    tests = TestReport()
    if junit_xml:
        try:
            parsed = parse_junit(junit_xml)
        except JUnitError as exc:
            warnings.append(str(exc))
        else:
            tests = TestReport(passed=parsed.passed, skipped=parsed.skipped)
            for (
                t
            ) in parsed.failed:  # scrub parsed fields (not the raw XML) so the markup stays valid
                msg = scrub(t.message)
                redactions.update(msg.counts)
                tests.failed.append(
                    FailedTest(class_name=t.class_name, name=t.name, message=msg.text)
                )

    location = _location(facts.top_frame, facts.failed_test_ids, tests, stage)
    return ProcessedFailure(
        failed_stage=stage,
        error_type=facts.error_type,
        error_message=facts.error_message,
        location=location,
        error_lines=facts.error_lines,
        stack_traces=facts.stack_traces,
        tests=tests,
        fingerprint=compute_fingerprint(facts.error_type, location, facts.error_message),
        redactions=dict(redactions),
        warnings=warnings,
    )


def _location(
    top_frame: str | None, test_ids: list[str], tests: TestReport, stage: str | None
) -> str:
    """Most specific stable location: project frame > failing test > stage."""
    if top_frame:
        return top_frame
    if test_ids:
        return min(test_ids)  # sorted pick keeps it independent of execution order
    if tests.failed:
        return min(f"{t.class_name}::{t.name}" for t in tests.failed)
    return f"stage:{stage}" if stage else "unknown"
