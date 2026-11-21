# ADR 0006: The ticket step is deterministic code, not an LLM agent

Status: accepted

## Context
The original design lists a `ticket_agent`. Ticketing is a template filled from validated results, a search by fingerprint, a CODEOWNERS lookup, and a create-or-comment decision. None of it needs language understanding or judgment.

## Decision
Implement it as plain code that runs under the `ticket_agent` identity: the same tool policy and audit log as the LLM agents, and the only holder of the Jira write tools. The model's output reaches Jira only as validated, scrubbed, escaped data inside a fixed template.

## Consequences
- Writes cannot be steered by text hidden in logs, commits or documents; there is no extra model call to pay for, retry or evaluate.
- Output is deterministic and tested byte for byte; idempotency and race handling are explicit code paths.
- Less flexible phrasing than a model would produce. If richer summaries are wanted later, an LLM can draft a *field* (for example a one-paragraph summary) that code validates and inserts, without ever holding a write tool.
