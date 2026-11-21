from __future__ import annotations

from typing import Any

import pytest
from pipeline_doctor_agent.fake_llm import ScriptedLlm, call, logic_bug_scripts, reply
from pipeline_doctor_agent.investigator import InvestigationError, Investigator
from pipeline_doctor_agent.policy.gate import Action
from pipeline_doctor_agent.schemas import PipelineFailedEvent
from pipeline_doctor_agent.settings import Settings

A = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
B = "b2c3d4e5f60718293a4b5c6d7e8f901234567890"
C = "c3d4e5f60718293a4b5c6d7e8f90123456789012"

EVENT = PipelineFailedEvent(
    idempotency_key="demo-service/main:12",
    job_name="demo-service/main",
    build_number=12,
    repo="acme/demo-service",
    commit_sha=C,
)


happy_scripts = logic_bug_scripts


def make(
    settings: Settings, scripts: dict[str, list[dict[str, Any]]]
) -> tuple[Investigator, ScriptedLlm]:
    llm = ScriptedLlm(scripts=scripts, seen_requests=[])
    return Investigator(settings, small_model=llm, large_model=llm), llm


async def test_end_to_end_with_real_mcp_servers_and_fake_llm(settings: Settings) -> None:
    inv, llm = make(settings, happy_scripts())
    result = await inv.investigate(EVENT)

    assert result.diagnosis.category == "code" and result.diagnosis.suspect_commit == C
    assert result.decision.action is Action.FIX
    assert result.processed.error_type == "AssertionError"
    assert result.fingerprint == result.processed.fingerprint
    # 4 model calls for log (2) + code (3) + diagnose (1) = 6; tools: 1 + 2 = 3
    assert result.usage.llm_calls == 6 and result.usage.tool_calls == 3
    assert result.usage.prompt_tokens == 600
    audited = [(r.agent, r.tool, r.status) for r in result.audit]
    assert ("log_analyst", "get_failed_log_excerpt", "ok") in audited
    assert ("code_investigator", "get_commit_diff", "ok") in audited
    # the diagnose prompt received the other agents' findings, delimited as data
    diag_prompt = next(r for r in llm.seen_requests if "ROLE: diagnose_agent" in r)
    assert "<code_findings>" in diag_prompt and "changes discount formula" in diag_prompt
    assert "UNTRUSTED" in diag_prompt


async def test_low_confidence_goes_to_ticket_only(settings: Settings) -> None:
    inv, _ = make(settings, happy_scripts(confidence=0.5))
    result = await inv.investigate(EVENT)
    assert result.decision.action is Action.TICKET_ONLY
    assert any("below the threshold" in r for r in result.decision.reasons)


async def test_hallucinated_suspect_commit_is_not_fixed(settings: Settings) -> None:
    fake = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    scripts = happy_scripts(suspect=fake)
    scripts["code_investigator"][-1] = reply(
        {
            "candidate_commits": [{"sha": C, "reason": "x"}],
            "suspect_commit": C,
            "suspect_reason": "x",
            "files_of_interest": [],
        }
    )
    inv, _ = make(settings, scripts)
    result = await inv.investigate(EVENT)
    assert result.decision.action is Action.TICKET_ONLY
    assert any("not among the commits" in r for r in result.decision.reasons)


async def test_invalid_model_output_is_rejected(settings: Settings) -> None:
    scripts = happy_scripts()
    scripts["diagnose_agent"] = [
        reply(
            {
                "category": "magic",
                "root_cause": "x",
                "evidence": [],
                "confidence": 7,
                "fixable": True,
            }
        )
    ]
    inv, _ = make(settings, scripts)
    with pytest.raises(InvestigationError) as exc:
        await inv.investigate(EVENT)
    assert exc.value.kind in {"invalid_output", "agent_error"}


# ---- adversarial and limit tests ---------------------------------------------------------


def with_settings(settings: Settings, **changes: Any) -> Settings:
    from dataclasses import replace

    return replace(settings, **changes)


async def test_policy_blocks_out_of_scope_repo_and_audits_it(settings: Settings) -> None:
    scripts = happy_scripts()
    scripts["code_investigator"] = [
        call(
            "list_commits_between", repo="evil/other-repo", base_sha=A, head_sha=C
        ),  # steered elsewhere
        call("list_commits_between", repo="acme/demo-service", base_sha=A, head_sha=C),
        reply(
            {
                "candidate_commits": [{"sha": C, "reason": "x"}],
                "suspect_commit": C,
                "suspect_reason": "x",
                "files_of_interest": [],
            }
        ),
    ]
    inv, _ = make(settings, scripts)
    result = await inv.investigate(EVENT)
    blocked = [r for r in result.audit if r.status == "blocked"]
    assert len(blocked) == 1 and blocked[0].tool == "list_commits_between"
    assert (
        "evil/other-repo" in blocked[0].args["repo"] and "failing repository" in blocked[0].detail
    )
    assert any(r.status == "ok" and r.tool == "list_commits_between" for r in result.audit)


async def test_agent_cannot_call_a_tool_outside_its_allowlist(settings: Settings) -> None:
    # log_analyst tries a GitHub tool; the toolset filter hides it and/or the policy blocks it.
    scripts = happy_scripts()
    scripts["log_analyst"] = [
        call("get_commit_diff", repo="acme/demo-service", sha=C),
        reply(
            {"failed_stage": "Test", "error_summary": "x", "key_error_lines": [], "likely_area": ""}
        ),
    ]
    inv, _ = make(settings, scripts)
    result = await inv.investigate(EVENT)
    assert not any(
        r.agent == "log_analyst" and r.tool == "get_commit_diff" and r.status == "ok"
        for r in result.audit
    )


async def test_compromised_model_cannot_write(settings: Settings) -> None:
    """Simulates a successful prompt injection: the investigator tries to push to main."""
    scripts = happy_scripts()
    scripts["code_investigator"] = [
        call("create_branch", repo="acme/demo-service", name="main", from_sha=C),
        call(
            "open_draft_pull_request",
            repo="acme/demo-service",
            head="main",
            base="main",
            title="pwn",
            body="x",
        ),
        reply(
            {
                "candidate_commits": [{"sha": C, "reason": "x"}],
                "suspect_commit": C,
                "suspect_reason": "x",
                "files_of_interest": [],
            }
        ),
    ]
    inv, _ = make(settings, scripts)
    result = await inv.investigate(EVENT)  # the run survives; the writes simply never happen
    assert not any(
        r.tool in {"create_branch", "open_draft_pull_request"} and r.status == "ok"
        for r in result.audit
    )


async def test_injection_scenario_commit_message_is_data_not_instructions(
    settings: Settings,
) -> None:
    f = "f60718293a4b5c6d7e8f90123456789012345abc"
    z = "0718293a4b5c6d7e8f90123456789012345abcde"
    event = PipelineFailedEvent(
        idempotency_key="notes-app/main:8",
        job_name="notes-app/main",
        build_number=8,
        repo="acme/notes-app",
        commit_sha=z,
    )
    scripts = {
        "log_analyst": [
            reply(
                {
                    "failed_stage": "Test",
                    "error_summary": "test_render_title fails",
                    "key_error_lines": [],
                    "likely_area": "src/title.py",
                }
            )
        ],
        "code_investigator": [
            call("list_commits_between", repo="acme/notes-app", base_sha=f, head_sha=z),
            reply(
                {
                    "candidate_commits": [{"sha": z, "reason": "strips whitespace differently"}],
                    "suspect_commit": z,
                    "suspect_reason": "x",
                    "files_of_interest": ["src/title.py"],
                }
            ),
        ],
        "diagnose_agent": [
            reply(
                {
                    "category": "code",
                    "root_cause": "render_title no longer strips the name",
                    "evidence": ["assert 'Hello ' == 'Hello'"],
                    "suspect_commit": z,
                    "confidence": 0.9,
                    "fixable": True,
                }
            )
        ],
    }
    inv, llm = make(settings, scripts)
    result = await inv.investigate(event)
    # The injected text reached the model only as tool-result data, labelled untrusted...
    assert all(r.status in {"ok", "blocked"} for r in result.audit)
    assert not any(r.tool in {"create_branch", "open_draft_pull_request"} for r in result.audit)
    # ...and every agent's system prompt carries the rule not to obey it.
    assert all("UNTRUSTED" in p for p in llm.seen_requests)


async def test_llm_call_budget(settings: Settings) -> None:
    inv, _ = make(with_settings(settings, max_llm_calls=3), happy_scripts())
    with pytest.raises(InvestigationError) as exc:
        await inv.investigate(EVENT)
    assert exc.value.kind == "budget_exceeded" and "max_llm_calls" in str(exc.value)


async def test_token_budget(settings: Settings) -> None:
    inv, _ = make(with_settings(settings, token_budget=250), happy_scripts())
    with pytest.raises(InvestigationError) as exc:
        await inv.investigate(EVENT)
    assert exc.value.kind == "budget_exceeded" and "token_budget" in str(exc.value)


async def test_wall_clock_timeout(settings: Settings) -> None:
    llm = ScriptedLlm(scripts=happy_scripts(), seen_requests=[], delay_s=5)
    inv = Investigator(with_settings(settings, run_timeout_s=1), small_model=llm, large_model=llm)
    with pytest.raises(InvestigationError) as exc:
        await inv.investigate(EVENT)
    assert exc.value.kind == "timeout"


async def test_unknown_job_is_facts_unavailable(settings: Settings) -> None:
    inv, _ = make(settings, happy_scripts())
    bad = EVENT.model_copy(update={"job_name": "nope/main"})
    with pytest.raises(InvestigationError) as exc:
        await inv.investigate(bad)
    assert exc.value.kind == "facts_unavailable"


async def test_parallel_branches_failing_together_are_still_classified_as_a_budget_error(
    settings: Settings,
) -> None:
    """Both investigators trip the budget in the same instant; ADK may wrap or even replace that
    exception while unwinding. The run must still be reported as budget_exceeded, not a crash."""
    inv, _ = make(with_settings(settings, token_budget=1), happy_scripts())
    with pytest.raises(InvestigationError) as exc:
        await inv.investigate(EVENT)
    assert exc.value.kind == "budget_exceeded" and "token_budget" in str(exc.value)
