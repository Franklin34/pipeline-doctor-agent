# ADR 0010: Pub/Sub as the cloud queue; Redis stays for idempotency

Status: accepted

## Context
Locally the queue is Redis Streams. In Google Cloud, running our own Redis queue would mean operating retry, redelivery and dead-letter logic that a managed service already provides.

## Decision
- **Pub/Sub carries the events.** Redelivery, backoff and the dead-letter topic are subscription configuration; the worker only acknowledges on success. Our code adds what Pub/Sub cannot know: a malformed event is dead-lettered immediately (retrying cannot help), and the worker's "final attempt" must equal the subscription's `max_delivery_attempts` so the fallback ticket is written on the last delivery.
- **Leases are extended while work runs.** A diagnosis plus a fix can outlast any ack deadline, so a background task extends it; a crashed worker stops extending and the message returns.
- **Redis stays** (Memorystore) for idempotency keys, locks and webhook de-duplication. Pub/Sub's delivery is at-least-once like Redis Streams', so those keys are what make duplicate deliveries harmless. Replacing them with Firestore or Spanner was considered and rejected as a rewrite with no gain here.
- **Ingress is public, authenticated by HMAC in code**; the worker takes no inbound traffic. The ingress and the worker have separate service accounts with resource-level grants only.
- **Secrets are containers in Terraform, values added by hand,** so they never enter state.
- **No automatic fixes on Cloud Run:** no Docker there. The cloud worker diagnoses and tickets.

## Consequences
- The worker cannot read queue depth from Pub/Sub the way it does from Redis Streams, so the `pd_queue_*` and `pd_dlq_messages` gauges are unset in the cloud; Cloud Monitoring alerts on the subscriptions replace the Prometheus queue alerts.
- Pub/Sub's minimum dead-letter attempts is 5; the local default of 3 would be rejected, which the consumer enforces.
- The Terraform has been validated and policy-tested but never applied.
