# services/ingress

The webhook entry point (Go). Receives a Jenkins failure notification, verifies it, drops duplicates, and queues a `PipelineFailed` event.

- **Authentication:** HMAC-SHA256 over the raw body, constant-time compare, fail closed (503 if the queue is unavailable so Jenkins retries).
- **Queue:** Redis Streams (default, local) or Pub/Sub (`QUEUE_BACKEND=pubsub`). Redis is also used for de-duplication.
- **Observability:** Prometheus metrics at `/metrics`, W3C `traceparent` written into every event so the worker continues the trace.

Details, API and failure behaviour: [docs/ingress.md](../../docs/ingress.md). Run the tests: `cd services/ingress && go test -race ./...`.
