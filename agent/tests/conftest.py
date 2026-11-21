from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pipeline_doctor_agent.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"


@pytest.fixture(scope="session")
def jenkins_bin(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Build the real Go Jenkins MCP server once; tests talk to it over stdio."""
    if shutil.which("go") is None:
        pytest.skip("go toolchain not installed")
    out = tmp_path_factory.mktemp("bin") / "jenkins-mcp"
    subprocess.run(
        ["go", "build", "-o", str(out), "./cmd/jenkins-mcp"],
        cwd=ROOT / "mcp-servers" / "jenkins",
        check=True,
        capture_output=True,
    )
    return str(out)


@pytest.fixture
def settings(jenkins_bin: str) -> Settings:
    return Settings(
        fixtures_dir=str(FIXTURES),
        jenkins_mcp_command=jenkins_bin,
        github_repo=None,
        run_timeout_s=60,
        fix_threshold=0.8,
    )


SHA_C = "c3d4e5f60718293a4b5c6d7e8f90123456789012"


@pytest.fixture
def make_result() -> Any:
    """Build a realistic InvestigationResult without running any agent (fast ticket tests)."""
    from pipeline_doctor_agent.investigator import InvestigationResult, Usage
    from pipeline_doctor_agent.policy.gate import decide
    from pipeline_doctor_agent.preprocess import preprocess
    from pipeline_doctor_agent.schemas import (
        Category,
        CodeFindings,
        CommitRef,
        Diagnosis,
        KnowledgeFindings,
        LogFindings,
        PipelineFailedEvent,
    )

    def build(
        *,
        category: str = "code",
        confidence: float = 0.92,
        fixable: bool = True,
        fingerprint: str | None = None,
        evidence: list[str] | None = None,
        root_cause: str = "apply_discount returns price * rate instead of price * (1 - rate)",
        suspect: str | None = SHA_C,
        in_range: bool = True,
        error_lines: list[str] | None = None,
        repo: str | None = "acme/demo-service",
        build_url: str | None = "https://jenkins.example/job/demo-service/main/12/",
        threshold: float = 0.8,
        failed_tests: list[tuple[str, str]] | None = None,
    ) -> InvestigationResult:
        event = PipelineFailedEvent(
            idempotency_key="demo-service/main:12",
            job_name="demo-service/main",
            build_number=12,
            repo=repo,
            commit_sha=SHA_C,
            build_url=build_url,
        )
        lines = error_lines or [
            "E       assert 10.0 == 90.0",
            "FAILED tests/test_pricing.py::test_apply_discount",
        ]
        processed = preprocess(
            ["[Pipeline] { (Test)", *lines, "ERROR: script returned exit code 1", "[Pipeline] }"],
            failed_stage="Test",
        )
        if fingerprint:
            processed = processed.model_copy(update={"fingerprint": fingerprint})
        if failed_tests:
            from pipeline_doctor_agent.preprocess import FailedTest, TestReport

            report = TestReport(
                failed=[FailedTest(class_name=c, name=n, message="x") for c, n in failed_tests]
            )
            processed = processed.model_copy(update={"tests": report})
        code = CodeFindings(
            candidate_commits=[
                CommitRef(sha=SHA_C if in_range else "a" * 40, reason="changes the formula")
            ],
            suspect_commit=suspect,
        )
        diagnosis = Diagnosis(
            category=Category(category),
            root_cause=root_cause,
            evidence=evidence
            or ["assert 10.0 == 90.0 in test_apply_discount", "commit c3d4e5f changed the formula"],
            suspect_commit=suspect,
            confidence=confidence,
            fixable=fixable,
        )
        return InvestigationResult(
            event=event,
            processed=processed,
            log_findings=LogFindings(error_summary="x", key_error_lines=[]),
            code_findings=code,
            knowledge_findings=KnowledgeFindings(),
            diagnosis=diagnosis,
            decision=decide(diagnosis, code, threshold=threshold),
            usage=Usage(6, 3, 600, 120),
            audit=(),
            duration_s=1.0,
        )

    return build
