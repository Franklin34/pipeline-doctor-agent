"""A scripted stand-in for the LLM, for unit tests and mock-mode evals (no network, no cost).

Each agent prompt starts with ``ROLE: <agent_name>``; the fake keeps one script per role and
plays its steps in order: a ``call`` step makes the agent call a tool, a ``reply`` step makes it
answer (a dict is sent as JSON). Running out of steps is a test failure, loudly.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models import BaseLlm, LlmCapabilities
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

_ROLE = re.compile(r"ROLE:\s*(\w+)")

A = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
B = "b2c3d4e5f60718293a4b5c6d7e8f901234567890"
C = "c3d4e5f60718293a4b5c6d7e8f90123456789012"


def call(tool: str, **args: Any) -> dict[str, Any]:
    return {"call": tool, "args": args}


def reply(content: dict[str, Any] | str) -> dict[str, Any]:
    return {"reply": content}


class ScriptedLlm(BaseLlm):
    model: str = "scripted"
    scripts: dict[str, list[dict[str, Any]]]
    prompt_tokens: int = 100
    output_tokens: int = 20
    delay_s: float = 0.0  # simulate a slow model (timeout tests)
    seen_requests: list[str] = Field(default_factory=list)  # system instruction of each call
    seen_messages: list[str] = Field(default_factory=list)  # the last user message of each call

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"scripted"]

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        system = str(llm_request.config.system_instruction) if llm_request.config else ""
        match = _ROLE.search(system)
        if match is None:
            raise AssertionError("prompt has no 'ROLE:' marker; ScriptedLlm cannot pick a script")
        role = match.group(1)
        self.seen_requests.append(system)
        user_texts = [
            p.text
            for c in llm_request.contents
            if c.role == "user"
            for p in (c.parts or [])
            if p.text
        ]
        self.seen_messages.append(user_texts[-1] if user_texts else "")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        script = self.scripts.get(role)
        if not script:
            raise AssertionError(f"ScriptedLlm ran out of steps for role '{role}'")
        step = script.pop(0)

        if "call" in step:
            part = types.Part(
                function_call=types.FunctionCall(name=step["call"], args=step["args"])
            )
        else:
            body = step["reply"]
            part = types.Part(text=body if isinstance(body, str) else json.dumps(body))
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=self.prompt_tokens, candidates_token_count=self.output_tokens
            ),
        )


def logic_bug_scripts(
    *, confidence: float = 0.92, suspect: str | None = C, category: str = "code"
) -> dict[str, list[dict[str, Any]]]:
    """Scripts for the `logic-bug` fixture scenario (a well-behaved model that gets it right)."""
    return {
        "log_analyst": [
            call("get_failed_log_excerpt", job="demo-service/main", build=12, max_lines=20),
            reply(
                {
                    "failed_stage": "Test",
                    "error_summary": "test_apply_discount asserts 90.0 but got 10.0",
                    "key_error_lines": ["E       assert 10.0 == 90.0"],
                    "likely_area": "app/pricing.py",
                }
            ),
        ],
        "code_investigator": [
            call("list_commits_between", repo="acme/demo-service", base_sha=A, head_sha=C),
            call("get_commit_diff", repo="acme/demo-service", sha=C),
            reply(
                {
                    "candidate_commits": [
                        {"sha": B, "reason": "README only"},
                        {"sha": C, "reason": "changes discount formula"},
                    ],
                    "suspect_commit": suspect,
                    "suspect_reason": "inverts the discount",
                    "files_of_interest": ["app/pricing.py"],
                }
            ),
        ],
        "diagnose_agent": [
            reply(
                {
                    "category": category,
                    "root_cause": "apply_discount returns price * rate instead of price * (1 - rate)",
                    "evidence": [
                        "assert 10.0 == 90.0 in test_apply_discount",
                        "commit c3d4e5f changed the formula",
                    ],
                    "suspect_commit": suspect,
                    "confidence": confidence,
                    "fixable": category in {"code", "test", "dependency"},
                }
            ),
        ],
    }


D = "d4e5f60718293a4b5c6d7e8f9012345678901234"
E = "e5f60718293a4b5c6d7e8f90123456789012345a"


def infra_scripts() -> dict[str, list[dict[str, Any]]]:
    """Scripts for the `infra-missing-service` fixture scenario: a README-only commit, Postgres down."""
    return {
        "log_analyst": [
            call("get_failed_log_excerpt", job="payments-api/main", build=30, max_lines=20),
            reply(
                {
                    "failed_stage": "Integration Test",
                    "error_summary": "integration tests cannot connect to postgres: connection refused",
                    "key_error_lines": ["E   psycopg2.OperationalError: ... Connection refused"],
                    "likely_area": "database service in the pipeline network",
                }
            ),
        ],
        "code_investigator": [
            call("list_commits_between", repo="acme/payments-api", base_sha=E, head_sha=D),
            reply(
                {
                    "candidate_commits": [{"sha": D, "reason": "README badge change only"}],
                    "suspect_commit": None,
                    "suspect_reason": "the only commit touches documentation",
                    "files_of_interest": [],
                }
            ),
        ],
        "diagnose_agent": [
            reply(
                {
                    "category": "infrastructure",
                    "root_cause": "The postgres service is unreachable from the Integration Test stage (connection refused); the only commit changed the README",
                    "evidence": [
                        "psycopg2.OperationalError: connection refused on port 5432",
                        "commit d4e5f60 only edits README.md",
                    ],
                    "suspect_commit": None,
                    "confidence": 0.9,
                    "fixable": False,
                }
            )
        ],
    }
