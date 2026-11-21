from __future__ import annotations

import pytest
from pipeline_doctor_agent.policy.tools import DEFAULT_ALLOWLIST, WRITE_TOOLS, RunScope, ToolPolicy

POLICY = ToolPolicy(RunScope(job_name="demo-service/main", repo="acme/demo-service"))


@pytest.mark.parametrize(
    ("agent", "tool", "args", "refused"),
    [
        ("log_analyst", "get_build", {"job": "demo-service/main", "build": 12}, False),
        (
            "log_analyst",
            "get_failed_log_excerpt",
            {"job": "demo-service/main", "build": 12, "max_lines": 50},
            False,
        ),
        (
            "code_investigator",
            "list_commits_between",
            {"repo": "acme/demo-service", "base_sha": "abc1234", "head_sha": "def5678"},
            False,
        ),
        (
            "code_investigator",
            "get_file",
            {"repo": "acme/demo-service", "path": "a.py", "ref": "main"},
            False,
        ),
        # wrong agent for the tool
        ("log_analyst", "get_commit_diff", {"repo": "acme/demo-service", "sha": "abc1234"}, True),
        ("code_investigator", "get_build", {"job": "demo-service/main", "build": 1}, True),
        # agents with no tools
        ("diagnose_agent", "get_build", {"job": "demo-service/main", "build": 1}, True),
        (
            "knowledge_agent",
            "get_file",
            {"repo": "acme/demo-service", "path": "a", "ref": "main"},
            True,
        ),
        # unknown agent
        ("rogue_agent", "get_build", {"job": "demo-service/main", "build": 1}, True),
        # scope pinning
        ("log_analyst", "get_build", {"job": "other/job", "build": 1}, True),
        ("code_investigator", "get_codeowners", {"repo": "evil/repo"}, True),
        # argument sanity
        (
            "code_investigator",
            "get_file",
            {"repo": "acme/demo-service", "path": "x" * 2000, "ref": "main"},
            True,
        ),
        ("log_analyst", "get_build", {"job": "demo-service/main", "build": {"nested": 1}}, True),
    ],
)
def test_policy(agent: str, tool: str, args: dict[str, object], refused: bool) -> None:
    assert (POLICY.check(agent, tool, args) is not None) is refused


JIRA_WRITES = {"create_issue", "add_comment"}
GIT_WRITES = {"create_branch", "commit_files", "open_draft_pull_request"}


def holders(tool: str) -> set[str]:
    return {a for a in DEFAULT_ALLOWLIST if POLICY.check(a, tool, {}) is None}


@pytest.mark.parametrize("tool", sorted(JIRA_WRITES))
def test_only_the_ticket_agent_can_write_to_jira(tool: str) -> None:
    assert holders(tool) == {"ticket_agent"}


@pytest.mark.parametrize("tool", sorted(GIT_WRITES))
def test_only_the_pr_publisher_can_write_to_git(tool: str) -> None:
    assert holders(tool) == {"pr_publisher"}


def test_nobody_can_rebuild_and_no_llm_agent_holds_any_write_tool() -> None:
    assert holders("rebuild") == set()
    llm_agents = {
        "log_analyst",
        "code_investigator",
        "knowledge_agent",
        "diagnose_agent",
        "fix_agent",
    }
    for tool in WRITE_TOOLS:
        assert not (holders(tool) & llm_agents), tool


def test_the_fix_llm_has_no_tools_at_all() -> None:
    assert DEFAULT_ALLOWLIST["fix_agent"] == frozenset()


def test_pr_publisher_may_send_file_lists_but_ticket_agent_may_not() -> None:
    files = [{"path": "app/a.py", "content": "x" * 1000}]
    args = {
        "repo": "acme/demo-service",
        "branch": "pipeline-doctor/x",
        "message": "m",
        "files": files,
    }
    assert POLICY.check("pr_publisher", "commit_files", args) is None
    assert (
        POLICY.check("ticket_agent", "create_issue", {"summary": "s", "files": files}) is not None
    )
    huge = [{"path": "a", "content": "x" * 2_000_000}]
    assert POLICY.check("pr_publisher", "commit_files", {**args, "files": huge}) is not None


def test_pr_publisher_is_pinned_to_the_failing_repository() -> None:
    assert POLICY.check("pr_publisher", "get_repo_info", {"repo": "evil/other"}) is not None


def test_argument_size_limit_is_per_agent() -> None:
    long = "x" * 5_000
    assert (
        POLICY.check("ticket_agent", "create_issue", {"description": long}) is None
    )  # ticket bodies are long
    assert POLICY.check("ticket_agent", "create_issue", {"description": "x" * 20_001}) is not None
    assert (
        POLICY.check(
            "code_investigator", "get_file", {"repo": "acme/demo-service", "path": long, "ref": "m"}
        )
        is not None
    )


def test_ticket_agent_can_write_tickets_but_only_for_the_failing_repo() -> None:
    assert POLICY.check("ticket_agent", "create_issue", {"summary": "s"}) is None
    assert POLICY.check("ticket_agent", "get_codeowners", {"repo": "acme/demo-service"}) is None
    assert POLICY.check("ticket_agent", "get_codeowners", {"repo": "evil/repo"}) is not None


def test_repo_argument_refused_when_run_has_no_repo() -> None:
    p = ToolPolicy(RunScope(job_name="j", repo=None))
    assert "no repository" in (
        p.check("code_investigator", "get_codeowners", {"repo": "a/b"}) or ""
    )


def test_refusal_message_names_the_correct_value() -> None:
    reason = POLICY.check("log_analyst", "get_build", {"job": "other/job", "build": 1})
    assert reason is not None and "demo-service/main" in reason


def test_knowledge_agent_may_only_search_the_knowledge_base() -> None:
    assert (
        POLICY.check(
            "knowledge_agent", "search_knowledge", {"query": "postgres refused", "kind": "any"}
        )
        is None
    )
    assert (
        POLICY.check("log_analyst", "search_knowledge", {"query": "x", "kind": "any"}) is not None
    )
    assert POLICY.check("diagnose_agent", "search_knowledge", {"query": "x"}) is not None
