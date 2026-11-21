# ADR 0002: Deterministic orchestration, LLM inside steps

Status: accepted

## Context
A failure investigation has a fixed shape: gather facts, investigate in parallel, diagnose, decide, then act. Letting a model choose the next step adds cost, variance and attack surface without adding value here.

## Decision
- Plain code does the control flow: pre-processing, a `SequentialAgent` over a `ParallelAgent` of investigators plus the diagnose agent, then the fix gate. The model reasons only inside each step, with schema-validated output.
- Anything that must be reliable (scrubbing, fingerprints, gating, limits, idempotency) is code, not prompt.

## Consequences
- Runs are reproducible, testable with a scripted fake LLM, and bounded in steps and cost.
- Less flexible than a free-form agent; new capabilities mean adding a step.
- **ADK note:** ADK 2.15 marks `ParallelAgent`/`SequentialAgent` as deprecated in favour of `Workflow`. We use them because they are supported and match the design; the agent construction is isolated in `agents.py` so a migration to `Workflow` touches one file. Revisit when they are scheduled for removal.
