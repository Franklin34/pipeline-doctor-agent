# ADR 0007: How automatic fixes stay safe

Status: accepted

## Context
Letting a model change code is the riskiest capability of the system. Its input (logs, commit messages, files) is attacker-influenced, and its output runs in a test process.

## Decision
- **Opt-in.** Fixes are off unless `FIX_ENABLED=true`; the default is diagnose and ticket.
- **The fix model has no tools.** Code builds its context and returns structured edits; code checks, applies, tests and publishes them. A fully manipulated model can only produce edits.
- **Edits, not diffs or files.** Exact-match search-and-replace on existing files is minimal, verifiable and gives precise feedback.
- **Reproduce before fixing,** and **judge after**: the tests must fail first, then pass, none may disappear, zero tests is never a pass.
- **Deterministic patch rules** plus **the same restrictions again inside the GitHub MCP server**, so no single layer is the only defence.
- **Hard limits:** at most 3 attempts (loop and model-call budget), a wall-clock timeout, one fix per failure, drafts only.
- **Real isolation, proven by tests** against real Docker (network off, non-root, read-only, no secrets, resource limits, kill on timeout, no leaked containers).

## Consequences
- Worst realistic outcomes are a wrong diagnosis or a rejected draft PR for a human to close; not a merge, not a change to CI or infrastructure, not exfiltration from the test run.
- Extra moving parts (Docker, a cache of installed dependencies) and a strict checklist the model must satisfy, which will reject some legitimate fixes. That bias is intentional.
- The install step needs network access; mitigated by wheels-only installs and package-name pinning, but not eliminated (see docs/sandbox-and-fixes.md).
