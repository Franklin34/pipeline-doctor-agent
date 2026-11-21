# ADR 0008: Observability, with content kept out of telemetry

Status: accepted

## Context
The system makes decisions nobody watches in real time: a worker that stalls, a model that loops, a queue that fills, or an agent steered by build output all look the same from outside, which is "no ticket appeared". Telemetry is also a data path: traces and logs often go to a third-party backend, and this system handles build logs and source code.

## Decision
- **Three signals, each with a job.** Metrics say *that* something is wrong (alerts, dashboard). Traces say *where* in one failure's life (webhook, queue wait, diagnosis, fix, ticket). Logs say *why* (JSON lines carrying the trace id).
- **One trace per failure, started by the ingress.** The Go ingress writes a W3C `traceparent` into the event; the worker continues it. ADK's agent, model and tool spans nest under ours because everything shares the global tracer provider.
- **Bounded labels.** Every metric label value comes from a closed set; anything else is folded into `other`. Job names, fingerprints and issue keys are never labels (unbounded series would take Prometheus down). Per-failure detail lives in traces and logs.
- **Content never enters telemetry.** Spans carry ids, categories and counts. A failing span records the exception *type*, not its message (messages can hold log text). `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` is forced to `false`; ADK's legacy span schema would otherwise write tool arguments and results onto spans. A test proves both that the switch matters and that we ship it off.
- **Telemetry cannot fail a run.** Queue sampling, a malformed `traceparent` or an exporter problem are swallowed after a debug log.
- **Alerts are tied to code by tests.** Every metric and label value used in an alert or dashboard panel must exist in the code (or the ingress' Go source), so renaming a metric breaks a test instead of silently blanking a panel.
- **Queue depth is measured by the worker**, from the consumer group (`pending`, `lag`) and the length of the dead-letter stream, plus a last-poll timestamp for liveness.

## Consequences
- Prometheus scrapes the worker on a port (`9102`, loopback by default). A pull endpoint is simplest and standard; a push gateway or OTLP metrics would be the move for short-lived jobs, which the worker is not.
- The cost figure is an estimate from configured prices, not a bill; prices default to 0 (unknown) instead of a guess that goes stale. The token-spike alert works without prices.
- Not yet verified against real Prometheus, Grafana and Jaeger processes (see docs/observability.md).
