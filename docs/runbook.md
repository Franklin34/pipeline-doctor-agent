# Operations runbook

For whoever is on call for a deployment of this system. Every alert in `deploy/observability/alerts.yml` and `deploy/terraform/monitoring.tf` links here. Cloud-specific commands assume `gcloud`; local ones assume the compose stack.

## First, find the story of one failure
Logs are JSON with `trace_id`. Take the `idempotency_key` of the Jenkins build (`<job>:<build>`) or the trace id from any log line, and search for it. In Jaeger, one trace covers webhook, queue wait, diagnosis, fix and ticket. The audit lines (`pipeline_doctor.audit`) list every tool call with its status; `blocked` means policy refused a call.

## Alerts

### Dead-letter queue not empty (`PipelineDoctorDlqNotEmpty`, Cloud Monitoring equivalent)
A message exhausted its retries, or was malformed. That pipeline failure has **no ticket**.
1. Read the entries. Redis: `redis-cli XRANGE pipeline-failed-dlq - +`. Pub/Sub: pull from the `*-dlq-reader` subscription (`gcloud pubsub subscriptions pull <name> --limit=10`; add `--auto-ack` only after saving them).
2. Each has the event (`data`), `attempts`, an `error` summary and `failed_at`. A `permanent:` error means the event was invalid (fix the sender). Anything else is usually a dependency that was down: the model provider, Jira, GitHub or Jenkins.
3. Fix the cause, then re-publish the event body to the main queue (`XADD pipeline-failed * data '<json>'`, or `gcloud pubsub topics publish`). Idempotency keys make it safe to replay an event that partly succeeded.

### Worker stalled or down
A live process that stops polling usually hangs on a model or MCP call. `RUN_TIMEOUT_SECONDS` should free it. Restarting is safe: unacknowledged messages are redelivered and every external write is idempotent. Check memory (a very large log) and the model provider's status.

### Queue backlog
Failures arrive faster than they are diagnosed. Add workers (the Cloud Run worker scales to 3; Redis consumers share one group). Check if a dependency is slow (the `pd_investigation_duration_seconds` histogram) before adding capacity.

### Investigation failures above 30%
Look at the `outcome` breakdown: `facts_unavailable` (Jenkins), `timeout`, `budget_exceeded` (limits too low or a loop), `agent_error`, `invalid_output` (the model stopped following the schema: a model change or a bad prompt). Fallback tickets are written on the last attempt, so people still hear about the failure; check `PipelineDoctorDegradedTickets`.

### Guardrail blocks or injection flags
A model asked for a tool it may not use, or input screening saw text addressed to an AI. Treat as a possible attack on the pipeline, not as noise.
1. Find the run (audit lines with `blocked`, or the ticket, which lists the flags).
2. Read the commit or log line that carried the text; find its author (a contributor, or a dependency's build output).
3. Nothing was written: policy refused the call and the run was held to ticket-only. If the text came from a compromised dependency, that is the real incident.

### Token spike, daily cost, run limits hit
A prompt change, a retry loop or a flood of failures. Compare `pd_investigations_total` with `pd_llm_tokens_total`. Lower `MAX_LLM_CALLS_PER_RUN` and `RUN_TOKEN_BUDGET` to cap damage while you investigate.

### Fixes failing
Check Docker on the worker host and the sandbox image, then the fix model's output (`FIX_MAX_ATTEMPTS` is capped at 3).

### Ingress down, publish errors, 5xx
Publish errors are answered with 503, so Jenkins retries; the queue (Redis or Pub/Sub) is unreachable. Nothing is lost while Jenkins keeps retrying; after its retries expire, events are.

## Routine tasks

**Turn automatic fixes on for a repository.** `FIX_ENABLED=true`, a Docker host, `GITHUB_MODE=real`, a token limited to that repository, and `GITHUB_ALLOWED_REPOS`. Turn on branch protection and required reviews on the default branch first; the agent only opens draft PRs, but protection is the second lock. Start with one repository.

**Rotate a secret.** Webhook secret: add a new version in Secret Manager, redeploy the ingress, update Jenkins. GitHub token: create the new fine-grained token, add a version, redeploy the worker, revoke the old one. Never paste tokens into chat, tickets or tracked files (`.env` is git-ignored).

**Add a runbook or past incident to the knowledge base.** Put a markdown file under `knowledge/` and run `make ingest`. Treat those files like code: anyone who can edit them can steer retrieval.

**Add a scenario to the golden dataset.** `fixtures/<id>/` with the four JSON files; run `make eval`. Do this after every production surprise.

**Measure the real model.** `make eval-live` with credentials (costs money). Compare with the scripted baseline's expectations before changing models or prompts.

**Stop everything safely.** Scale the worker to zero (or stop it); events queue up and wait (seven days on Pub/Sub). The ingress keeps accepting. To stop *writes only*, set `FIX_ENABLED=false`; tickets still flow.

## Cost levers
Model calls dominate. `MAX_LLM_CALLS_PER_RUN`, `RUN_TOKEN_BUDGET` and `SMALL_MODEL`/`LARGE_MODEL` control them; every distinct failure event costs one investigation. A recurring failure is de-duplicated at the *ticket* (a comment on the existing issue), not before the model runs, so a noisy pipeline still costs a diagnosis per build. In Google Cloud the fixed costs are Memorystore and the always-on worker.
