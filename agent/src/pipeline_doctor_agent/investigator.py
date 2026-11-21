"""One investigation run: facts -> parallel investigators -> diagnosis -> fix gate."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from google.adk.agents.run_config import RunConfig
from google.adk.models import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, ValidationError

from pipeline_doctor_agent.agents import build_pipeline, github_toolset, jenkins_toolset
from pipeline_doctor_agent.audit import AuditLog, AuditRecord
from pipeline_doctor_agent.facts import (
    FactSource,
    FactsUnavailable,
    McpFactSource,
    build_user_message,
)
from pipeline_doctor_agent.guards import RunGuard
from pipeline_doctor_agent.knowledge import Searcher, make_search_tool
from pipeline_doctor_agent.limits import BudgetExceeded, RunBudget
from pipeline_doctor_agent.observability import metrics, span
from pipeline_doctor_agent.policy.gate import GateDecision, decide
from pipeline_doctor_agent.policy.tools import RunScope, ToolPolicy
from pipeline_doctor_agent.preprocess import ProcessedFailure
from pipeline_doctor_agent.schemas import (
    CodeFindings,
    Diagnosis,
    KnowledgeFindings,
    LogFindings,
    PipelineFailedEvent,
    parse_model_json,
)
from pipeline_doctor_agent.screening import Flag, dedupe, screen
from pipeline_doctor_agent.settings import Settings

log = logging.getLogger("pipeline_doctor.investigator")
APP_NAME = "pipeline_doctor"


class InvestigationError(RuntimeError):
    """A run failed in a way that is reported, not hidden. ``kind`` feeds metrics (Phase 8)."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind


@dataclass(frozen=True)
class Usage:
    llm_calls: int
    tool_calls: int
    prompt_tokens: int
    output_tokens: int

    @property
    def steps(self) -> int:
        return self.llm_calls + self.tool_calls


@dataclass(frozen=True)
class InvestigationResult:
    event: PipelineFailedEvent
    processed: ProcessedFailure
    log_findings: LogFindings
    code_findings: CodeFindings
    knowledge_findings: KnowledgeFindings
    diagnosis: Diagnosis
    decision: GateDecision
    usage: Usage
    audit: tuple[AuditRecord, ...]
    duration_s: float
    injection_flags: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        return self.processed.fingerprint


ToolsetFactory = Callable[[Settings], tuple[list[Any], list[Any]]]


def default_toolsets(settings: Settings) -> tuple[list[Any], list[Any]]:
    return [jenkins_toolset(settings)], [github_toolset(settings)]


class Investigator:
    def __init__(
        self,
        settings: Settings,
        *,
        small_model: str | BaseLlm | None = None,
        large_model: str | BaseLlm | None = None,
        fact_source: FactSource | None = None,
        toolsets: ToolsetFactory = default_toolsets,
        searcher: Searcher | None = None,
    ) -> None:
        self.settings = settings
        self.small_model = small_model or settings.small_model
        self.large_model = large_model or settings.large_model
        self.fact_source = fact_source or McpFactSource(settings)
        self.toolsets = toolsets
        self.searcher = searcher

    async def facts_for(self, event: PipelineFailedEvent) -> ProcessedFailure | None:
        """Pre-processed facts without any LLM call; None if Jenkins is unreachable. Used to write a
        useful fallback ticket when the model is unavailable."""
        try:
            return (await asyncio.wait_for(self.fact_source.gather(event), timeout=30)).processed
        except Exception:
            log.warning("facts unavailable for fallback ticket", exc_info=True)
            return None

    async def investigate(self, event: PipelineFailedEvent) -> InvestigationResult:
        """One investigation, traced and counted. The work itself is in ``_investigate``."""
        stats = _RunStats()
        started = time.monotonic()
        m = metrics.current()
        with span("pipeline_doctor.investigate") as sp:
            try:
                result = await self._investigate(event, stats)
            except InvestigationError as exc:
                m.investigation(exc.kind)
                sp.set_attribute("pd.outcome", exc.kind)
                if (b := stats.budget) is not None:
                    m.usage(
                        prompt=b.prompt_tokens,
                        output=b.output_tokens,
                        steps=b.steps,
                        duration_s=time.monotonic() - started,
                    )
                    if b.tripped is not None:
                        m.budget_trip(b.tripped.limit)
                raise
            m.investigation("ok")
            m.diagnosis(result.diagnosis.category, result.decision.action)
            u = result.usage
            m.usage(
                prompt=u.prompt_tokens,
                output=u.output_tokens,
                steps=u.steps,
                duration_s=result.duration_s,
            )
            sp.set_attributes(
                {
                    "pd.outcome": "ok",
                    "pd.category": str(result.diagnosis.category),
                    "pd.action": str(result.decision.action),
                    "pd.confidence": result.diagnosis.confidence,
                    "pd.steps": u.steps,
                    "pd.tokens": u.prompt_tokens + u.output_tokens,
                }
            )
            return result

    async def _investigate(
        self, event: PipelineFailedEvent, stats: _RunStats
    ) -> InvestigationResult:
        started = time.monotonic()
        s = self.settings
        run_id = uuid.uuid4().hex[:12]
        repo = event.repo or s.github_repo

        # 1. Deterministic facts and pre-processing: no LLM yet.
        try:
            facts = await asyncio.wait_for(self.fact_source.gather(event), timeout=s.run_timeout_s)
        except FactsUnavailable as exc:
            raise InvestigationError("facts_unavailable", str(exc)) from exc
        except TimeoutError as exc:
            raise InvestigationError("timeout", "fetching build facts timed out") from exc

        # 2. Guard rails shared by every agent in this run.
        budget = RunBudget(
            max_llm_calls=s.max_llm_calls,
            max_tool_calls=s.max_llm_calls * 3,
            token_budget=s.token_budget,
        )
        stats.budget = budget
        audit = AuditLog(run_id=run_id)
        guard = RunGuard(
            policy=ToolPolicy(RunScope(job_name=event.job_name, repo=repo)),
            audit=audit,
            budget=budget,
        )
        jenkins_tools, github_tools = self.toolsets(s)
        pipeline = build_pipeline(
            small_model=self.small_model,
            large_model=self.large_model,
            guard=guard,
            jenkins_tools=jenkins_tools,
            github_tools=github_tools,
            knowledge_tools=[make_search_tool(self.searcher)] if self.searcher else None,
        )

        # 3. Run the agents under the wall-clock limit.
        sessions = InMemorySessionService()
        runner = Runner(app_name=APP_NAME, agent=pipeline, session_service=sessions)
        session = await sessions.create_session(
            app_name=APP_NAME, user_id="doctor", session_id=run_id
        )
        message = types.Content(
            role="user", parts=[types.Part(text=build_user_message(event, facts, repo))]
        )
        try:
            async with asyncio.timeout(s.run_timeout_s):
                async for _ in runner.run_async(
                    user_id="doctor",
                    session_id=session.id,
                    new_message=message,
                    run_config=RunConfig(
                        max_llm_calls=s.max_llm_calls * 2
                    ),  # backstop behind our own counter
                ):
                    pass
        except TimeoutError as exc:
            raise InvestigationError("timeout", f"run exceeded {s.run_timeout_s:.0f}s") from exc
        except Exception as exc:
            exceeded = budget.tripped or _find(exc, BudgetExceeded)
            if exceeded is not None:
                raise InvestigationError("budget_exceeded", str(exceeded)) from exc
            raise InvestigationError("agent_error", f"{type(exc).__name__}: {exc}") from exc
        finally:
            await _close(jenkins_tools + github_tools)

        # 4. Validate every output; invalid output is rejected, never repaired by guessing.
        state = await sessions.get_session(app_name=APP_NAME, user_id="doctor", session_id=run_id)
        if state is None:  # cannot happen with the in-memory service; fail loudly if it ever does
            raise InvestigationError("agent_error", "session disappeared")
        try:
            logf = parse_model_json(LogFindings, state.state["log_findings"])
            codef = parse_model_json(CodeFindings, state.state["code_findings"])
            knowf = parse_model_json(KnowledgeFindings, state.state["knowledge_findings"])
            diagnosis = parse_model_json(Diagnosis, state.state["diagnosis"])
        except (KeyError, ValidationError, ValueError) as exc:
            raise InvestigationError(
                "invalid_output", f"{type(exc).__name__}: {str(exc)[:300]}"
            ) from exc

        # 5. The gate: plain code decides what may happen next.
        flags = dedupe([*_screen_facts(facts.processed), *guard.flags])
        for flag in flags:
            metrics.current().injection_flag(flag.kind)
        decision = decide(
            diagnosis,
            codef,
            threshold=s.fix_threshold,
            injection_flags=tuple(str(f) for f in flags),
        )
        result = InvestigationResult(
            event=event,
            processed=facts.processed,
            log_findings=logf,
            code_findings=codef,
            knowledge_findings=knowf,
            diagnosis=diagnosis,
            decision=decision,
            usage=Usage(
                budget.llm_calls, budget.tool_calls, budget.prompt_tokens, budget.output_tokens
            ),
            audit=tuple(audit.records),
            duration_s=time.monotonic() - started,
            injection_flags=tuple(str(f) for f in flags),
        )
        log.info(
            "investigation complete",
            extra={
                "run_id": run_id,
                "category": diagnosis.category,
                "confidence": diagnosis.confidence,
                "action": decision.action,
                "steps": result.usage.steps,
            },
        )
        return result


def _screen_facts(p: ProcessedFailure) -> list[Flag]:
    text = "\n".join(
        [
            *p.error_lines,
            *p.stack_traces,
            p.error_message or "",
            *(f"{t.name} {t.message}" for t in p.tests.failed),
        ]
    )
    return screen(text, "build_output")


@dataclass
class _RunStats:
    """What a failed run still reports: the budget it used before it stopped."""

    budget: RunBudget | None = None


def _find[E: BaseException](exc: BaseException, kind: type[E]) -> E | None:
    """Look for ``kind`` anywhere in an exception's causes, contexts and exception groups.

    Iterative with one visited set: parallel branches can fail together and ADK nests the failures
    in groups whose members chain back to each other, which a naive recursive walk never leaves.
    """
    stack: list[BaseException] = [exc]
    seen: set[int] = set()
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, kind):
            return cur
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        stack.extend(e for e in (cur.__cause__, cur.__context__) if e is not None)
    return None


async def _close(toolsets: list[Any]) -> None:
    for t in toolsets:
        close = getattr(t, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # cleanup must never mask the real outcome
                log.warning("toolset close failed", exc_info=True)


__all__ = ["BaseModel", "InvestigationError", "InvestigationResult", "Investigator", "Usage"]
