"""Typed outputs of deterministic pre-processing."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FailedTest(BaseModel):
    class_name: str
    name: str
    message: str


class TestReport(BaseModel):
    __test__ = False  # not a pytest class

    passed: int = 0
    skipped: int = 0
    failed: list[FailedTest] = Field(default_factory=list)


class ProcessedFailure(BaseModel):
    """Everything the LLM stage may see about a failure. All text is already scrubbed."""

    failed_stage: str | None
    error_type: str | None = Field(description="Normalized exception/assertion type, if found")
    error_message: str | None = Field(description="First line of the primary error")
    location: str = Field(description="Top project frame, failing test id, or stage")
    error_lines: list[str]
    stack_traces: list[str]
    tests: TestReport
    fingerprint: str = Field(description="16 hex chars, stable across runs of the same failure")
    redactions: dict[str, int] = Field(description="Secrets removed, by kind (never the values)")
    warnings: list[str] = Field(default_factory=list)
