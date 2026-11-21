# Observability

Three signals, one design (ADR 0008): **metrics** say that something is wrong, **traces** say where in one failure's life, **logs** say why.

## Metrics (worker, `:9102/metrics`)

| Metric | Labels | Answers |
|---|---|---|
| `pd_events_total` | `result`: processed, duplicate, failed, dead_lettered | Is work flowing? |
| `pd_investigations_total` | `outcome`: ok, facts_unavailable, timeout, budget_exceeded, agent_error, invalid_output | Why do runs fail? |
| `pd_diagnoses_total` | `category`, `action` | What kinds of failures, and what did the gate allow? |
| `pd_investigation_duration_seconds`, `pd_investigation_steps` | histogram | How long, how many model plus tool calls? |
| `pd_llm_tokens_total` | `kind`: prompt, output | Cost driver |
| `pd_llm_cost_usd_total` | | Estimated spend (needs `PRICE_IN_PER_MTOK`, `PRICE_OUT_PER_MTOK`) |
| `pd_budget_trips_total` | `limit` | Runs stopped by a step, call or token limit |
| `pd_tool_calls_total` | `agent`, `status`: ok, error, blocked | What do agents do? |
| `pd_guardrail_blocks_total` | `agent` | A model asked for something the policy refused |
| `pd_injection_flags_total` | `kind` | Build output or tool results that try to instruct the model |
| `pd_fix_attempts_total`, `pd_fix_duration_seconds` | `status` | Fix outcomes |
| `pd_tickets_total` | `action`, `degraded` | Tickets written, and fallbacks |
| `pd_queue_pending_messages`, `pd_queue_lag_messages`, `pd_dlq_messages` | gauges | Queue depth and dead letters |
| `pd_worker_last_poll_timestamp_seconds` | gauge | Worker liveness |

The ingress exports its own (`ingress_webhook_requests_total{code}`, `ingress_webhook_duration_seconds`, `ingress_webhook_duplicates_total`, `ingress_publish_errors_total`).

**Label rule:** values come from closed sets in `observability/metrics.py`; anything else becomes `other`. Never add a job name, build number, fingerprint or text as a label.

## Traces

One trace per failure. The ingress sets `traceparent` in the event; the worker continues it:

```
(ingress) webhook
  pipeline_doctor.event             attempt, job, build, result, ticket action
    pipeline_doctor.investigate     outcome, category, action, confidence, steps, tokens
      invoke_agent / generate_content / execute_tool     (ADK; model, tokens, tool name)
      MCP send tools/call ...                            (ADK; the MCP request)
    pipeline_doctor.fix             status, attempts
    pipeline_doctor.ticket          fingerprint
```

**No content in spans.** Attributes are ids, categories and counts. Failing spans record the exception type only. `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false` is set at startup (ADK's legacy schema would otherwise attach tool arguments and results). Set `OTEL_EXPORTER_OTLP_ENDPOINT` to export; unset keeps spans in-process, and logs still get trace ids.

## Logs

JSON, one object per line, fields from `extra=` preserved and clipped to 500 characters; `trace_id` and `span_id` are added inside a span. Find a failure's whole story with its trace id.

## Alerts (`deploy/observability/alerts.yml`)

| Alert | Fires when | Severity |
|---|---|---|
| DlqNotEmpty | any dead-lettered message for 5 min | page |
| WorkerStalled / WorkerDown | no queue poll for 2 min / not scrapeable | page |
| QueueBacklog | over 20 waiting or in flight for 10 min | ticket |
| InvestigationFailures | over 30% of runs fail (at least 5 runs) | ticket |
| DegradedTickets | a fallback ticket was written | ticket |
| TokenSpike | token rate three times the 6 h baseline | ticket |
| DailyCostHigh | over 25 USD in 24 h (edit to your budget) | ticket |
| RunLimitsHit | more than 3 runs stopped by a limit in an hour | ticket |
| GuardrailBlocks | a model's tool call was refused by policy | ticket |
| InjectionFlagged | input screening saw text that tries to instruct the model | ticket |
| FixesFailing | over 70% of fix attempts fail (at least 4) | ticket |
| IngressDown / PublishErrors / ServerErrors | ingress health | page / page / ticket |

## Run it locally

```bash
docker compose -f deploy/compose/docker-compose.yml --profile observability up -d   # Prometheus :9090, Grafana :3000, Jaeger :16686
METRICS_ADDR=0.0.0.0 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 uv run python -m pipeline_doctor_agent.worker
```

The Grafana dashboard ("Pipeline Doctor") is provisioned from `deploy/observability/grafana/dashboards/`.

## What is and isn't verified
- Verified by tests: metric values through the real queue, handler, investigator and ticket flow; trace continuation from the event's `traceparent`; no content in spans (with a control that proves the test can see a leak); JSON logs; the `/metrics` endpoint over HTTP; every alert and dashboard query uses metrics and label values that exist in the code.
- **Not yet verified against real Prometheus, Grafana or Jaeger processes**: the PromQL has never been parsed by Prometheus (CI runs `promtool check rules`, but it has not run on GitHub), and the dashboard JSON has never been loaded by Grafana. Do that with the compose profile before relying on it.

## With Pub/Sub (cloud)
The worker cannot read queue depth from Pub/Sub, so `pd_queue_pending_messages`, `pd_queue_lag_messages` and `pd_dlq_messages` stay unset and `pd_worker_last_poll_timestamp_seconds` is the only queue signal it exports. The Prometheus alerts `PipelineDoctorDlqNotEmpty` and `PipelineDoctorQueueBacklog` therefore do not apply; `deploy/terraform/monitoring.tf` defines the Cloud Monitoring equivalents on the subscriptions' own metrics (`num_undelivered_messages` on the dead-letter subscription, `oldest_unacked_message_age` on the worker's).
