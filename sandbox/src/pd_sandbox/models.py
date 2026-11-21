"""Structured results of a sandbox run."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TestSummary(BaseModel):
    __test__ = False  # not a pytest class

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failed_tests: list[str] = Field(
        default_factory=list, description="'module.Class::name' of failing tests"
    )

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    @property
    def broken(self) -> int:
        return self.failed + self.errors


class SandboxResult(BaseModel):
    exit_code: int | None = Field(description="None when the run was killed (timeout)")
    timed_out: bool = False
    duration_s: float
    tests: TestSummary = Field(default_factory=TestSummary)
    output_excerpt: str = Field(default="", description="Tail of the combined output; NOT scrubbed")

    @property
    def passed(self) -> bool:
        """Tests ran, none failed, and the process exited cleanly. Zero tests is never a pass."""
        return (
            self.exit_code == 0
            and not self.timed_out
            and self.tests.total > 0
            and self.tests.broken == 0
        )

    @property
    def reproduces_failure(self) -> bool:
        return not self.timed_out and self.exit_code not in (0, None) and self.tests.broken > 0


class SandboxError(RuntimeError):
    """The sandbox itself failed (Docker missing, dependency install failed): not a test failure."""
