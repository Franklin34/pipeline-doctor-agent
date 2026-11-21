"""The fix agent: one LLM call per attempt, no tools, structured output only."""

from __future__ import annotations

import uuid
from typing import Protocol

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import ValidationError

from pipeline_doctor_agent.agents import SECURITY_RULES
from pipeline_doctor_agent.guards import RunGuard
from pipeline_doctor_agent.limits import BudgetExceeded
from pipeline_doctor_agent.models import resolve_model
from pipeline_doctor_agent.schemas import FixProposal, parse_model_json

FIX_INSTRUCTION = """\
ROLE: fix_agent. You propose a minimal change to the code that makes a failing test pass.
{rules}

You cannot run code and you have no tools: you only write edits. An automated system checks them
against strict rules, applies them, and runs the tests in an isolated sandbox.

How to write edits:
- Each edit replaces `old_text` with `new_text` in an EXISTING file. `old_text` must appear EXACTLY
  ONCE in that file: copy it exactly, including indentation, with enough surrounding lines to be unique.
- Change as little as possible (at most 5 edits). Fix the code under test, not the test, unless the
  diagnosis says the test itself is wrong.
- Never edit pipeline, CI, Docker, infrastructure or configuration files. Never skip, delete or weaken
  a test. Never add network access, subprocesses, eval/exec or file-writing code.
- If a previous attempt failed, read why and change your approach instead of repeating it.
- `explanation` is one or two sentences for the reviewing engineer."""


class FixProposalError(RuntimeError):
    """The model produced no usable proposal (invalid output or a run limit was hit)."""


class Proposer(Protocol):
    async def propose(self, message: str) -> FixProposal: ...


class AdkProposer:
    """Runs a fresh single-turn LlmAgent per call. The guard's budget spans all attempts, so the
    number of model calls is capped at the attempt limit by two independent mechanisms."""

    def __init__(self, model: str | BaseLlm, guard: RunGuard) -> None:
        self.model, self.guard = model, guard

    async def propose(self, message: str) -> FixProposal:
        g = self.guard
        agent = LlmAgent(
            name="fix_agent",
            model=resolve_model(self.model),
            instruction=FIX_INSTRUCTION.format(rules=SECURITY_RULES),
            output_schema=FixProposal,
            output_key="fix_proposal",
            before_model_callback=g.before_model,
            after_model_callback=g.after_model,
            before_tool_callback=g.before_tool,
            after_tool_callback=g.after_tool,
        )
        sessions = InMemorySessionService()
        runner = Runner(app_name="pipeline_doctor_fix", agent=agent, session_service=sessions)
        sid = uuid.uuid4().hex
        await sessions.create_session(
            app_name="pipeline_doctor_fix", user_id="doctor", session_id=sid
        )
        try:
            async for _ in runner.run_async(
                user_id="doctor",
                session_id=sid,
                new_message=types.Content(role="user", parts=[types.Part(text=message)]),
            ):
                pass
        except Exception as exc:
            if g.budget.tripped is not None:
                raise FixProposalError(f"run limit reached: {g.budget.tripped}") from exc
            if isinstance(exc, BudgetExceeded):
                raise FixProposalError(f"run limit reached: {exc}") from exc
            raise FixProposalError(
                f"model call failed: {type(exc).__name__}: {str(exc)[:200]}"
            ) from exc
        session = await sessions.get_session(
            app_name="pipeline_doctor_fix", user_id="doctor", session_id=sid
        )
        try:
            return parse_model_json(FixProposal, (session.state if session else {})["fix_proposal"])
        except (KeyError, ValidationError, ValueError) as exc:
            raise FixProposalError(
                f"invalid proposal: {type(exc).__name__}: {str(exc)[:200]}"
            ) from exc
