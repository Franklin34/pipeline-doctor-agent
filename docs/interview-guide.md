# Interview guide

The questions this project is most likely to prompt, with short answers and where to look. Answers are about this repository; where something was not verified, the answer says so. Do not claim more than [verification.md](verification.md) allows.

## The 60-second pitch
Pipeline Doctor turns a failed Jenkins build into a diagnosis, a Jira ticket and, if enabled and safe, a sandbox-tested draft pull request. The interesting part is not the LLM; it is making an agent that reads untrusted text and can write to systems **safe**: the model has no write tools, code enforces every limit, and humans merge. I measured the system around the model with a scripted one, including models that lie or obey injections; I have not yet measured a real model's diagnostic quality.

## Architecture and design
**Why deterministic orchestration instead of a free-roaming agent?** Control flow is a fixed pipeline (parallel investigators, then diagnosis, then a gate). The model reasons inside each step but never decides what happens next. That makes runs repeatable, bounded in cost and testable. [ADR 0002](adr/0002-deterministic-orchestration.md)

**What is the one idea behind the safety design?** The model proposes, code decides. Every control (tool allowlists, scope pinning, gate, patch rules, judge, branch and path restrictions) is code a persuaded model cannot edit. [ADR 0003](adr/0003-policy-gate-in-code.md)

**Why is the ticket step plain code, not an agent?** Ticketing is a template, a search and a create-or-comment decision; it needs no judgment. As code it cannot be steered by text hidden in a log, costs no model call, and is tested byte for byte. [ADR 0006](adr/0006-ticket-step-is-code.md)

**Why two queues (Redis Streams and Pub/Sub)?** One interface; Redis is simple locally, Pub/Sub gives managed retry and dead-lettering in the cloud. Redis stays for idempotency keys either way. [ADR 0010](adr/0010-pubsub-transport-and-cloud-run.md)

**Why MCP servers, and why one per system?** A narrow, schema-validated tool surface with compact outputs and server-side restrictions that hold even if the agent is compromised. Jenkins is in Go to show the protocol is language-neutral. [docs/mcp-servers.md](mcp-servers.md)

## Reliability
**What if the same event arrives twice, or two builds fail the same way at once?** At-least-once delivery means everything is idempotent: per-event done and lease keys, a per-fingerprint ticket lock, comment idempotency markers, and `SET NX` claims for fixes. The second occurrence becomes a comment, not a ticket or a PR.

**What if the model is down?** Retries with the queue's backoff; on the final attempt a fallback ticket is written from the pre-processed facts (no model), so the failure is never silent.

**What if a worker dies mid-run?** Unacknowledged messages are redelivered; leases and claims expire; every external write is idempotent.

## Security
**How do you stop prompt injection?** You cannot stop a model being persuaded, so you limit what a persuaded model can do: no write tools, per-agent allowlists, arguments pinned to the failing job and repo, server-side re-checks, a gate that needs an in-range suspect commit, and human review. Screening flags suspicious text but is a tripwire. [threat model](threat-model.md)

**How do you know your tests can fail?** Mutation tests switch off screening, the policy layer, the confidence threshold and the scrubber, and assert the evaluation reports exactly that failure. [evals](../evals/tests/test_evals.py)

**What does the sandbox protect against, and what not?** Hostile test code reaching the network, host files or secrets, exhausting resources, escalating privileges. Not a container-runtime exploit, and its install step has network access (wheels only). [sandbox doc](sandbox-and-fixes.md)

**Why a draft PR and never an auto-merge?** The agent can be wrong in ways tests do not catch. A draft costs a reviewer a minute and keeps accountability human.

## Observability and evals
**Why three signals?** Metrics say that something is wrong, traces say where in one failure's life, logs say why. [ADR 0008](adr/0008-observability-design.md)

**How do you avoid leaking build data into telemetry?** Spans carry ids and counts only; failing spans record the error type; ADK's content capture is forced off (a test shows it would leak otherwise); metric labels come from closed sets.

**What do your evals prove?** With a scripted model, that everything around the model behaves, including a lying or obedient model. They do not measure diagnosis quality; that needs the real model (`make eval-live`, not yet run). Hard safety gates must be perfect; accuracy is compared to a baseline. [ADR 0009](adr/0009-evals-and-input-screening.md)

## Honest weaknesses (say these before they are asked)
- I have not measured a real model's accuracy.
- Jira and Jenkins were never run for real; GCP was never applied.
- Screening is heuristic; a determined attacker can paraphrase around it.
- A confident, in-range, wrong diagnosis can still produce a draft PR (the backstop is tests and review).
- The cloud worker cannot run fixes (no Docker on Cloud Run).

## Things I got wrong and fixed (good stories)
- A regex scrubber that took minutes on `a.a.a.…` input (ReDoS): bounded quantifiers, size caps, regression tests.
- MCP errors arrived wrapped in an anyio `ExceptionGroup`, hiding the real cause; a helper now unwraps them.
- A search that returned five sections of one runbook: added a per-document diversity cap.
- A CI action tag (`setup-uv@v10`) that does not exist, found only when pinning to SHAs.
- A `.gitignore` with no Terraform entries (state holds the Redis password).
- My first eval run passed everything immediately, which is why the mutation tests exist.
- A Pub/Sub dead-letter topic without a subscription silently drops messages.

## Questions to ask back
What would you want measured before trusting an agent with write access? Where would you put the human in the loop? How would you run the sandbox on your platform?
