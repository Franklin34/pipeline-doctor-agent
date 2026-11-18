# ADR 0004: Queue interface with Redis Streams and Pub/Sub adapters

Status: accepted

## Context
Failures must be processed reliably and decoupled from the webhook request. Local development should need no cloud account; production runs on Google Cloud.

## Decision
- A small `queue.Queue` interface (`Publish`, `Consume`, `Ping`) carrying opaque bytes.
- Redis Streams adapter for local use: consumer groups, native delivery counts for retries, `XAUTOCLAIM` for redelivery, a dead-letter stream after `MAX_ATTEMPTS`.
- Cloud Pub/Sub adapter (Phase 10) with a dead-letter topic configured in Terraform.
- Delivery is at-least-once; consumers are idempotent via `idempotency_key`.

## Consequences
- Ingress is fast and independent from agent latency or outages.
- Duplicates are possible (webhook retries, redelivery, a crash between DLQ write and ack), so idempotency is mandatory downstream.
- Two adapters must keep the same semantics; shared behavior is covered by tests per adapter.
