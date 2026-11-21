"""Run one scenario through the real investigator and record what happened."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pipeline_doctor_agent.fake_llm import ScriptedLlm
from pipeline_doctor_agent.investigator import InvestigationError, Investigator
from pipeline_doctor_agent.schemas import PipelineFailedEvent
from pipeline_doctor_agent.settings import Settings

from pd_evals.scenario import Scenario


@dataclass
class Outcome:
    scenario: str
    error: str | None = None  # InvestigationError kind, if the run failed
    error_detail: str = ""  # short, for the report (the message of an InvestigationError)
    category: str | None = None
    action: str | None = None
    suspect: str | None = None
    flags: list[str] = field(default_factory=list)
    blocked_calls: int = 0
    steps: int = 0
    tokens: int = 0
    secrets_seen: list[str] = field(default_factory=list)  # expected-absent strings found anywhere


def build_jenkins_mcp(root: Path) -> str:
    """Build the Go Jenkins MCP server into a temp dir (the eval talks to the real server)."""
    go = shutil.which("go")
    if go is None:
        raise RuntimeError("the go toolchain is needed to build the Jenkins MCP server")
    out = Path(tempfile.mkdtemp(prefix="pd-evals-")) / "jenkins-mcp"
    subprocess.run(  # noqa: S603
        [go, "build", "-o", str(out), "./cmd/jenkins-mcp"],
        cwd=root / "mcp-servers" / "jenkins",
        check=True,
        capture_output=True,
    )
    return str(out)


async def run_scenario(scenario: Scenario, settings: Settings, *, live: bool) -> Outcome:
    """``live=False`` plays the scenario's scripted model (tests everything *around* the model);
    ``live=True`` uses the real model from ``settings`` (measures the model, costs money)."""
    llm = None if live else ScriptedLlm(scripts=copy.deepcopy(scenario.scripted_model))
    investigator = (
        Investigator(settings)
        if llm is None
        else Investigator(settings, small_model=llm, large_model=llm)
    )
    out = Outcome(scenario.id)
    try:
        result = await investigator.investigate(PipelineFailedEvent(**scenario.event))
    except InvestigationError as exc:
        out.error = exc.kind
        out.error_detail = str(exc)[:400].replace("\n", " ")
        seen = _everything_the_model_saw(llm)
    else:
        out.category = str(result.diagnosis.category)
        out.action = str(result.decision.action)
        out.suspect = result.diagnosis.suspect_commit
        out.flags = list(result.injection_flags)
        out.blocked_calls = sum(1 for r in result.audit if r.status == "blocked")
        out.steps, out.tokens = (
            result.usage.steps,
            result.usage.prompt_tokens + result.usage.output_tokens,
        )
        produced = json.dumps(
            [
                result.diagnosis.model_dump(mode="json"),
                result.log_findings.model_dump(mode="json"),
                result.code_findings.model_dump(mode="json"),
                [r.__dict__ for r in result.audit],
                result.decision.reasons,
            ],
            default=str,
        )
        seen = _everything_the_model_saw(llm) + produced
    out.secrets_seen = [s for s in scenario.expected.secrets_absent if s in seen]
    return out


def _everything_the_model_saw(llm: ScriptedLlm | None) -> str:
    if llm is None:  # live mode: only the outputs can be checked
        return ""
    return "\n".join([*llm.seen_requests, *llm.seen_messages])
