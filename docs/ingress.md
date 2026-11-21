# Ingress service

`services/ingress` (Go) turns a Jenkins webhook into a queued `PipelineFailed` event and answers `202` in milliseconds. It never calls Jenkins, GitHub or an LLM.

## Request flow

1. Cap the body (`MAX_BODY_BYTES`, default 1 MiB) -> `413`.
2. Verify `X-Signature-256: sha256=<hex>` (HMAC-SHA256 of the raw body, constant-time compare) -> `401`. Nothing is parsed before this passes.
3. Parse JSON -> `400`; validate fields strictly -> `422`.
4. Non-`FAILURE` results -> `200 {"status":"ignored"}`.
5. Claim idempotency key `job_name:build_number` with Redis `SET NX` + TTL. Duplicate -> `202 {"status":"duplicate"}`. Redis unavailable -> `503` (fail closed: Jenkins retries).
6. Publish the event (with W3C `traceparent`). On failure the key is released and the response is `503`, so Jenkins' retry is not mistaken for a duplicate.
7. `202 {"status":"accepted"}`.

## Webhook payload

```json
{"job_name":"demo/main","build_number":42,"status":"FAILURE",
 "build_url":"https://jenkins.example/job/demo/42/","repo":"acme/demo","commit_sha":"abc1234"}
```

Sign it: `printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SHARED_SECRET"` and send `X-Signature-256: sha256=<hex>`.

## Queue contract (Redis Streams)

| Item | Value |
|---|---|
| Stream | `pipeline-failed` (field `data` = JSON `PipelineFailed`) |
| Consumer group | `pipeline-doctor` |
| Dead-letter stream | `pipeline-failed-dlq` (fields: `data`, `source_id`, `attempts`, `error`, `failed_at`) |
| Retries | Redis delivery count (`XPENDING`); unacked messages are re-claimed with `XAUTOCLAIM` after `MinIdle` |
| Dead-lettering | when the delivery count reaches `MAX_ATTEMPTS`, or a reclaimed message already exceeds it |

Delivery is **at least once**: consumers must be idempotent (use `idempotency_key`). The Go `queue.Queue` interface (`Publish`, `Consume`, `Ping`) is transport-agnostic; the Pub/Sub publisher is in `internal/queue/pubsub` (selected with `QUEUE_BACKEND=pubsub`). The Python worker (Phase 4) implements the same protocol.

## Endpoints

`POST /webhooks/jenkins`, `GET /healthz` (liveness), `GET /readyz` (pings Redis), `GET /metrics`.

Metrics: `ingress_webhook_requests_total{code}`, `ingress_webhook_duplicates_total`, `ingress_publish_errors_total`, `ingress_webhook_duration_seconds`.

## Configuration

`WEBHOOK_SHARED_SECRET` (required, >= 16 bytes), `INGRESS_ADDR`, `REDIS_URL`, `QUEUE_STREAM`, `IDEMPOTENCY_TTL`, `MAX_BODY_BYTES`, `OTEL_EXPORTER_OTLP_ENDPOINT` (empty = no export). See `.env.example`.

## Run locally

```sh
make test-go                       # unit tests use miniredis, no Docker needed
cp .env.example .env && make up    # Redis + ingress via Docker Compose
```
