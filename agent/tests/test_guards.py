from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pipeline_doctor_agent.audit import AuditLog, redact
from pipeline_doctor_agent.guards import UNTRUSTED_NOTICE, RunGuard
from pipeline_doctor_agent.limits import BudgetExceeded, RunBudget
from pipeline_doctor_agent.policy.tools import RunScope, ToolPolicy

GH = "ghp_" + "A1b2C3d4" * 5


def guard(**budget: int) -> RunGuard:
    b = RunBudget(
        max_llm_calls=budget.get("llm", 5),
        max_tool_calls=budget.get("tools", 5),
        token_budget=budget.get("tokens", 1000),
    )
    return RunGuard(
        policy=ToolPolicy(RunScope("demo-service/main", "acme/demo-service")),
        audit=AuditLog("r1"),
        budget=b,
    )


def ctx(agent: str) -> Any:
    return SimpleNamespace(agent_name=agent)


def tool(name: str) -> Any:
    return SimpleNamespace(name=name)


def test_blocked_call_returns_error_and_is_audited() -> None:
    g = guard()
    out = g.before_tool(
        tool("create_branch"),
        {"repo": "acme/demo-service", "name": "main"},
        ctx("code_investigator"),
    )
    assert out is not None and "refused by policy" in out["error"]
    rec = g.audit.records[0]
    assert (rec.agent, rec.tool, rec.status) == ("code_investigator", "create_branch", "blocked")


def test_allowed_call_passes_through() -> None:
    g = guard()
    assert (
        g.before_tool(
            tool("get_build"), {"job": "demo-service/main", "build": 1}, ctx("log_analyst")
        )
        is None
    )


def test_tool_results_are_scrubbed_and_labelled_untrusted() -> None:
    g = guard()
    result = {
        "content": [{"text": f"clone failed token={GH} PASSWORD=hunter2hunter2"}],
        "structuredContent": {"lines": [f"url postgres://u:pw123456@h/db {GH}"]},
    }
    out = g.after_tool(
        tool("get_failed_log_excerpt"),
        {"job": "demo-service/main", "build": 1},
        ctx("log_analyst"),
        result,
    )
    dumped = str(out)
    assert GH not in dumped and "hunter2hunter2" not in dumped and "pw123456" not in dumped
    assert out["notice"] == UNTRUSTED_NOTICE and out["data_from_tool"] == "get_failed_log_excerpt"
    assert g.audit.records[0].status == "ok"


def test_error_results_are_audited_as_errors() -> None:
    g = guard()
    g.after_tool(
        tool("get_build"),
        {"job": "demo-service/main", "build": 99},
        ctx("log_analyst"),
        {"isError": True, "content": [{"text": "not found"}]},
    )
    assert g.audit.records[0].status == "error"


def test_audit_never_stores_secrets_or_huge_values() -> None:
    g = guard()
    g.before_tool(
        tool("get_file"),
        {"repo": "acme/demo-service", "path": f"x/{GH}", "ref": "main"},
        ctx("code_investigator"),
    )
    g.after_tool(
        tool("get_file"),
        {"repo": "acme/demo-service", "path": f"x/{GH}", "ref": "main"},
        ctx("code_investigator"),
        {},
    )
    assert GH not in str(g.audit.records[-1].args)
    assert len(redact("y" * 5000)) < 400


def test_budget_counts_and_raises() -> None:
    g = guard(llm=2)
    g.before_model(ctx("a"), SimpleNamespace())  # type: ignore[arg-type]
    g.before_model(ctx("a"), SimpleNamespace())  # type: ignore[arg-type]
    with pytest.raises(BudgetExceeded) as exc:
        g.before_model(ctx("a"), SimpleNamespace())  # type: ignore[arg-type]
    assert exc.value.limit == "max_llm_calls"


def test_tool_call_budget_and_token_budget() -> None:
    g = guard(tools=1)
    g.before_tool(tool("get_build"), {"job": "demo-service/main", "build": 1}, ctx("log_analyst"))
    with pytest.raises(BudgetExceeded):
        g.before_tool(
            tool("get_build"), {"job": "demo-service/main", "build": 1}, ctx("log_analyst")
        )
    b = RunBudget(max_llm_calls=9, max_tool_calls=9, token_budget=100)
    b.on_usage(60, 30)
    with pytest.raises(BudgetExceeded) as exc:
        b.on_usage(10, 1)
    assert exc.value.limit == "token_budget" and b.total_tokens == 101
