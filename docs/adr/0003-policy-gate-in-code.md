# ADR 0003: Policy gate and write restrictions enforced in code

Status: accepted

## Context
Logs, commit messages, diffs and tickets are written by people we do not control, and a model reads them. Prompt instructions are not a security boundary.

## Decision
Every decision with consequences is made by deterministic code the model cannot influence:
- **Fix gate** (`policy/gate.py`): category, fixable, confidence threshold, and suspect commit present in the investigated range.
- **Tool policy** (`policy/tools.py`): per-agent allowlists, job/repo scope pinning, argument checks, run in `before_tool_callback`.
- **MCP servers** enforce write restrictions themselves (branch prefix, never the default branch, draft PRs only, one Jira project).
- **Run limits** and a **wall-clock timeout** bound cost and time.
- Tool output is re-scrubbed and labelled untrusted; every call is audited.

## Consequences
- A fully manipulated model can still produce a wrong diagnosis, but cannot write to the repository, reach another repo or job, or exceed its budget.
- Policy rules are table-tested, including a brute-force test that only the all-conditions-met combination reaches the fixer.
- More code to maintain than a prompt; that is the point.
