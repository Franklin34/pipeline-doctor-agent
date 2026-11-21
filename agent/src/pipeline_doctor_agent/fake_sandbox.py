"""A stand-in for the Docker sandbox for unit tests and mock-mode evals: it "runs" the tests of the
`acme__demo-service` fixture repository by reading the workspace, with no Docker involved. (The real
sandbox's isolation is proven separately, against real Docker, in sandbox/tests.)"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from pd_sandbox import SandboxError, SandboxResult, TestSummary


def demo_service_tests(workspace: Path) -> SandboxResult:
    """Pass/fail exactly as pytest would for the fixture: pricing tests need `price * (1 - rate)`."""
    pricing = (workspace / "app" / "pricing.py").read_text()
    fixed = "(1 - rate)" in pricing and "price * rate" not in pricing.replace(
        "price * (1 - rate)", ""
    )
    names: list[tuple[str, bool]] = []
    for f in sorted((workspace / "tests").glob("test_*.py")):
        for fn in re.findall(r"^def (test_\w+)", f.read_text(), flags=re.M):
            ok = fixed or not f.name.startswith("test_pricing")
            names.append((f"tests.{f.stem}::{fn}", ok))
    failed = sorted(n for n, ok in names if not ok)
    passed = sum(1 for _, ok in names if ok)
    return SandboxResult(
        exit_code=1 if failed else (0 if names else 5),
        duration_s=0.1,
        tests=TestSummary(passed=passed, failed=len(failed), failed_tests=failed),
        output_excerpt="\n".join(f"FAILED {n}" for n in failed) or f"{passed} passed",
    )


class FakeSandbox:
    image = "fake-sandbox:test"

    def __init__(
        self,
        runner: Callable[[Path], SandboxResult] = demo_service_tests,
        *,
        prepare_error: str | None = None,
    ) -> None:
        self.runner, self.prepare_error = runner, prepare_error
        self.prepared: list[str | None] = []
        self.runs = 0
        self.delay_s = 0.0

    async def prepare(self, requirements: str | None) -> Path:
        if self.prepare_error:
            raise SandboxError(self.prepare_error)
        self.prepared.append(requirements)
        return Path("/nonexistent-deps")

    async def run_tests(self, workspace: Path, deps: Path) -> SandboxResult:
        import asyncio

        self.runs += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return self.runner(workspace)
