from __future__ import annotations

import json
from pathlib import Path

import pytest
from pipeline_doctor_agent.preprocess import ProcessedFailure, preprocess

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
GH = "ghp_" + "A1b2C3d4" * 5


def load(scenario: str) -> tuple[list[str], str | None, ProcessedFailure]:
    job = json.loads((FIXTURES / scenario / "jenkins.json").read_text())
    build = next(b for b in job["builds"] if b["result"] == "FAILURE")
    log = job["logs"][str(build["number"])]
    return log, build["failed_stage"], preprocess(log, failed_stage=build["failed_stage"])


def test_logic_bug_fixture() -> None:
    _, _, r = load("logic-bug")
    assert r.failed_stage == "Test"
    assert r.error_type == "AssertionError"
    assert r.location == "tests/test_pricing.py::test_apply_discount"
    assert r.error_message == "assert 10.0 == 90.0"
    assert any("assert 10.0 == 90.0" in line for line in r.error_lines)
    assert not any("Cloning" in line for line in r.error_lines)  # other stages excluded


def test_infra_fixture_matches_seeded_jira_fingerprint() -> None:
    _, _, r = load("infra-missing-service")
    assert r.failed_stage == "Integration Test"
    assert r.error_type == "psycopg2.OperationalError"
    assert "Connection refused" in (r.error_message or "")
    assert r.fingerprint == "fdacb5a1cf4e5ebe"  # DEMO-7 in fixtures/infra-missing-service/jira.json


def test_injection_text_is_just_data_and_does_not_change_the_diagnosis_inputs() -> None:
    _, _, r = load("prompt-injection")
    assert r.error_type == "AssertionError"
    assert r.location == "tests/test_title.py::test_render_title"


def test_stage_is_inferred_when_not_given() -> None:
    log, _, expected = load("logic-bug")
    assert preprocess(log).failed_stage == "Test"
    assert preprocess(log).fingerprint == expected.fingerprint


def test_missing_stage_falls_back_to_whole_log_with_warning() -> None:
    log, _, expected = load("logic-bug")
    r = preprocess(log, failed_stage="Nope")
    assert r.fingerprint == expected.fingerprint
    assert any("not found" in w for w in r.warnings)


def test_secrets_never_reach_any_output_field() -> None:
    log = [
        "[Pipeline] { (Deploy)",
        f"+ git clone https://x-access-token:{GH}@github.com/acme/app.git",
        "+ export DB_PASSWORD=hunter2hunter2",
        "ERROR: deploy failed with token=" + GH,
        "ValueError: bad config password=hunter2hunter2",
        "[Pipeline] }",
    ]
    r = preprocess(log, failed_stage="Deploy")
    dumped = r.model_dump_json()
    assert GH not in dumped and "hunter2hunter2" not in dumped
    assert sum(r.redactions.values()) >= 3
    assert GH not in json.dumps(r.redactions)


def test_python_traceback_gives_project_frame_and_ignores_line_numbers() -> None:
    def make(line: int, host: str) -> list[str]:
        return [
            "[Pipeline] { (Test)",
            "Traceback (most recent call last):",
            f'  File "/var/jenkins_home/workspace/demo_main/app/service.py", line {line}, in load_config',
            "    return parse(path)",
            '  File "/usr/lib/python3.13/site-packages/yaml/__init__.py", line 81, in parse',
            f"ValueError: cannot read config from {host}:5432",
            "ERROR: script returned exit code 1",
            "[Pipeline] }",
        ]

    a = preprocess(make(10, "10.0.0.5"), failed_stage="Test")
    b = preprocess(make(42, "10.9.9.9"), failed_stage="Test")
    assert (
        a.location == "app/service.py:load_config"
    )  # workspace prefix stripped, library frame skipped
    assert a.error_type == "ValueError"
    assert a.stack_traces and "load_config" in a.stack_traces[0]
    assert a.fingerprint == b.fingerprint  # line number and IP changed, same failure


def test_java_trace() -> None:
    log = [
        "[Pipeline] { (Build)",
        'Exception in thread "main" java.lang.NullPointerException: x is null',
        "\tat com.acme.Foo.bar(Foo.java:10)",
        "\tat com.acme.Main.main(Main.java:5)",
        "ERROR: script returned exit code 1",
        "[Pipeline] }",
    ]
    r = preprocess(log, failed_stage="Build")
    assert r.error_type == "java.lang.NullPointerException"
    assert len(r.stack_traces) == 1 and "Foo.bar" in r.stack_traces[0]


def test_junit_is_parsed_and_scrubbed() -> None:
    xml = (
        '<testsuite><testcase classname="tests.a" name="test_x">'
        f'<failure message="auth failed password=hunter2hunter2 {GH}"/></testcase>'
        '<testcase classname="tests.a" name="test_y"/></testsuite>'
    )
    r = preprocess(
        ["[Pipeline] { (Test)", "ERROR: script returned exit code 1", "[Pipeline] }"], junit_xml=xml
    )
    assert r.tests.passed == 1 and len(r.tests.failed) == 1
    assert "hunter2hunter2" not in r.tests.failed[0].message and GH not in r.tests.failed[0].message
    assert (
        r.location == "tests.a::test_x"
    )  # falls back to the JUnit failure when the log has no test id


def test_bad_junit_degrades_gracefully() -> None:
    r = preprocess(["[Pipeline] { (Test)", "ERROR: x", "[Pipeline] }"], junit_xml="<broken")
    assert r.tests.failed == [] and any("JUnit" in w for w in r.warnings)


def test_empty_and_garbage_input() -> None:
    for log in ("", "\n\n", "\x00\x01 binary \xff"):
        r = preprocess(log)
        assert r.location == "unknown" and r.fingerprint
    big = preprocess("ERROR " + "x" * 10_000)
    assert all(len(line) < 500 for line in big.error_lines)


@pytest.mark.parametrize("scenario", ["logic-bug", "infra-missing-service", "prompt-injection"])
def test_fixtures_have_distinct_fingerprints(scenario: str) -> None:
    prints = {
        s: load(s)[2].fingerprint
        for s in ("logic-bug", "infra-missing-service", "prompt-injection")
    }
    assert len(set(prints.values())) == 3
    assert prints[scenario]
