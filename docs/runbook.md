# Operations runbook

_The full runbook is written in Phase 11. These sections exist now because alerts link to them._

## Dead-letter queue (alert: PipelineDoctorDlqNotEmpty)
A message is dead-lettered after `MAX_ATTEMPTS` failed deliveries, or at once if it is malformed.
1. Read the entries: `redis-cli XRANGE pipeline-failed-dlq - +`. Each has `data` (the event), `attempts`, `error` (a short reason) and `failed_at`.
2. Find the story in logs by the event's `idempotency_key`, or by trace id if the event has a `traceparent`.
3. Decide: a `permanent:` error means the event was invalid (fix the sender). Anything else is usually a dependency that was down (model provider, Jira, GitHub, Jenkins).
4. After fixing the cause, re-publish the `data` field with `XADD pipeline-failed * data '<json>'`, then delete the dead entry with `XDEL`.
No failure is silently lost, but until you do this, that pipeline failure has no ticket.

## Worker stalled or down
Check the process and its last log lines. A stalled worker with a live process usually means a model or MCP call that ignores its timeout; the run timeout (`RUN_TIMEOUT_SECONDS`) should free it. Restarting is safe: unacknowledged messages are redelivered and every external write is idempotent.
