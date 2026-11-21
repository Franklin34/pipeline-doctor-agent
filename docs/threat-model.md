# Threat model

Pipeline Doctor reads untrusted text (build logs, commit messages, documents), asks a model to reason about it, and can then write: tickets always, and, if enabled, a draft pull request built from code the model proposed. That combination is the whole risk. This page says what we protect, who might attack, what stops each attack, which test proves it, and what is left.

## Assets
1. **Source code and the default branch** of the repositories it can write to.
2. **Secrets** that appear in logs and environments: tokens, passwords, keys.
3. **The CI system and its credentials** (the agent never holds Jenkins write access).
4. **Integrity of the tickets**: people act on them, so a ticket that lies is an attack too.
5. **Cost and availability**: model spend, queue health.

## Who can attack, and through what
| Actor | Controls | Can reach us through |
|---|---|---|
| Contributor or compromised dependency | a commit, a test name, a log line, a package's install output | build log, commit message, diff, file content |
| Attacker with a stolen webhook secret | HTTP to the ingress | forged failure events |
| Malicious or buggy **model output** | whatever the model emits (a poisoned model is the same as a successful injection) | tool calls, diagnoses, fix proposals |
| Malicious fix proposal | code that will run in tests | the sandbox |
| Insider with access to telemetry | traces, logs, dashboards | observability data |

**Trust boundaries:** (1) internet to ingress; (2) build data to the model; (3) model to tools; (4) model-proposed code to the sandbox; (5) the system to GitHub, Jira, the model provider; (6) the system to its telemetry backend.

## Threats and controls
The rule behind every row: **the model proposes, code decides.** A model that is fully compromised by an injection should still be unable to do harm, so no control relies on the model behaving.

| # | Threat | Control (where) | Evidence (test) | Residual risk |
|---|---|---|---|---|
| T1 | Injected text in a log or commit tells the model to take over | Untrusted text fenced and labelled; the model has no write tools; per-agent allowlists (`policy/tools.py`) | `test_security.py` matrix; eval `obedient-to-injection` | A persuaded model can still write a *misleading diagnosis*; a human reads every ticket and reviews every PR |
| T2 | Model calls a tool it should not (push to `main`, create branch, rebuild) | Allowlist per agent; the GitHub MCP server independently refuses non-`pipeline-doctor/` branches, protected paths, non-draft PRs | policy matrix; `mcp-servers/github` tests; eval hard gate `forbidden_call_allowed` | None for the listed writes; a *new* tool must be classified (a test forces it) |
| T3 | Model targets another repository or job | `job` and `repo` arguments pinned to the failing event | `test_security.py` scope attacks | None known |
| T4 | Overconfident or hallucinated diagnosis causes an automatic fix | Gate in code: category, `fixable`, confidence threshold, suspect commit must be in the investigated range | eval `hallucinated-suspect`, `ambiguous-low-confidence`; `test_gate.py` | A confident, in-range, wrong diagnosis still reaches the sandbox; tests and review are the backstop |
| T5 | Injection detected late | Input screening flags instruction-like text; a flagged run can never be auto-fixed and the ticket says why | 25 attack and 15 benign phrasings; evals; mutation test | **Heuristic.** Paraphrase, other languages and encodings can slip through. It is a tripwire; T1 to T4 are the guarantee |
| T6 | Secret reaches the model, a ticket, a trace or a log | Scrub at the MCP server and again in the agent; bounded length; spans carry no content; failing spans record the error type only | scrubber tests; eval `expired-credentials` (secret absent from everything the model saw); `test_observability.py` | Unknown secret formats (the scrubber knows patterns, not values) |
| T7 | Malicious fix proposal runs code | Edits only, checked by patch rules; tests run in a container with no network, read-only filesystem, non-root, no capabilities, resource limits | 13 real-Docker isolation tests; `docs/sandbox-and-fixes.md` | Install step has network (wheels only); a kernel or runtime escape is out of scope |
| T8 | Fix "passes" by deleting or skipping tests | Judge requires tests ran, none removed, previously failing now pass; patch rules reject skips | sandbox and fix tests | A fix that is subtly wrong but green |
| T9 | Forged failure event | HMAC-SHA256 over raw bytes, constant-time compare; idempotency; schema validation; fail closed | ingress tests | Secret theft (rotate; Phase 10 uses a secret manager) |
| T10 | Replay and duplicates (double PRs, double tickets) | Idempotency keys, per-fingerprint locks and claims | worker and fix tests | Redis loss resets the keys; Jira and GitHub operations are themselves idempotent where they can be |
| T11 | Runaway loop or cost | Step, tool, token and time budgets; fix attempts capped at 3; alerts on spikes | `test_guards.py`; alerts `TokenSpike`, `RunLimitsHit` | Provider-side price changes |
| T12 | Cross-job poisoning through the knowledge base | Only repository runbooks and past incidents are indexed; results labelled untrusted; diversity cap per document | RAG tests | Anyone who can edit those documents can steer retrieval; protect them like code |
| T13 | A tool description or result tries to reconfigure the agent (MCP tool poisoning) | Our MCP servers are local and ours; tool results scrubbed, labelled and screened; descriptions wrapped as untrusted by ADK | `test_guards.py`; screening | Third-party MCP servers (not used) would need review |
| T14 | Denial of service: huge logs, regex bombs | Size caps; bounded quantifiers; screening time test; ingress body limit | preprocess and screening timing tests | Volume attacks need rate limiting at the edge (Phase 10) |

## What we do not claim
- **No guarantee against a wrong diagnosis.** The system narrows what a wrong or manipulated diagnosis can *do*; it does not make diagnoses correct. Quality is measured by `make eval-live`, which has not yet been run.
- **Screening is not a security boundary.** It lowers the chance a flagged failure is auto-fixed and gives security a signal.
- **The sandbox is not a hypervisor.** It stops hostile test code; it does not stop a container-runtime exploit.
- **Not yet verified:** real Gemini behaviour under injection (the scripted model can only simulate "obedient"), real Jira and GitHub permissions, the production deployment.

## Review checklist when adding a capability
1. Does it give an LLM agent a new tool? Classify it in `tests/test_security.py` and decide who holds it.
2. Does it write anywhere? Make it idempotent, scoped to the failing job/repo, and enforced again by the server.
3. Does new text reach a model? Scrub it, fence it, screen it.
4. Does it add telemetry? No content, bounded labels.
5. Add a scenario to the golden dataset and, if it is a new safeguard, a mutation test that proves the evaluation notices when it breaks.
