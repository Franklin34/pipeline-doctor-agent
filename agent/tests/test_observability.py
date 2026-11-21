"""Phase 8: metrics, traces, logs, and the dashboard and alert files that depend on them."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import pytest
import yaml
from mcp.client import Client
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pd_mcp_github.backend import MockBackend as GitHubMock
from pd_mcp_github.server import build_server as github_server
from pd_mcp_jira.backend import MockBackend as JiraMock
from pd_mcp_jira.server import build_server as jira_server
from pipeline_doctor_agent.audit import AuditLog
from pipeline_doctor_agent.fake_llm import ScriptedLlm, logic_bug_scripts, reply
from pipeline_doctor_agent.investigator import Investigator
from pipeline_doctor_agent.observability import (
    JsonFormatter,
    Metrics,
    install,
    setup_tracing,
    span,
)
from pipeline_doctor_agent.observability.metrics import current
from pipeline_doctor_agent.observability.server import start_metrics_server
from pipeline_doctor_agent.observability.tracing import context_from_traceparent
from pipeline_doctor_agent.settings import Settings
from pipeline_doctor_agent.ticket import TicketService
from pipeline_doctor_agent.worker.consumer import Delivery, StreamConsumer
from pipeline_doctor_agent.worker.handler import EventHandler

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
STREAM, GROUP = "pipeline-failed", "pipeline-doctor"
TRACE_ID = "a" * 32
EVENT = {
    "idempotency_key": "demo-service/main:12",
    "job_name": "demo-service/main",
    "build_number": 12,
    "repo": "acme/demo-service",
    "commit_sha": "c3d4e5f60718293a4b5c6d7e8f90123456789012",
    "build_url": "https://jenkins.example/job/demo-service/main/12/",
    "received_at": "2026-11-12T12:00:00Z",
    "traceparent": f"00-{TRACE_ID}-{'b' * 16}-01",
}


@pytest.fixture(autouse=True)
def metrics() -> Iterator[Metrics]:
    previous = current()
    yield install(Metrics(price_in_per_mtok=1.0, price_out_per_mtok=4.0))
    install(previous)


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    exp = InMemorySpanExporter()
    setup_tracing(extra_processors=[SimpleSpanProcessor(exp)])
    return exp


@pytest.fixture
def spans(exporter: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    exporter.clear()
    yield exporter
    exporter.clear()


@pytest.fixture
async def redis() -> Any:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


def value(m: Metrics, name: str, **labels: str) -> float:
    return m.registry.get_sample_value(name, labels) or 0.0


# ---- the metrics object -----------------------------------------------------------------------


def test_label_values_outside_the_known_set_fold_into_other(metrics: Metrics) -> None:
    """A model or a bug can never create a new time series by inventing a label value."""
    metrics.investigation("some-new-error-kind")
    metrics.guardrail_block("made-up-agent-name-" + "x" * 50)
    metrics.fix("weird", 1.0)
    assert value(metrics, "pd_investigations_total", outcome="other") == 1
    assert value(metrics, "pd_guardrail_blocks_total", agent="other") == 1
    assert value(metrics, "pd_fix_attempts_total", status="other") == 1


def test_cost_estimate_uses_the_configured_prices(metrics: Metrics) -> None:
    metrics.usage(prompt=2_000_000, output=500_000, steps=5, duration_s=3.0)
    assert value(metrics, "pd_llm_cost_usd_total") == pytest.approx(2.0 * 1.0 + 0.5 * 4.0)
    assert value(metrics, "pd_llm_tokens_total", kind="prompt") == 2_000_000
    assert (
        value(Metrics(), "pd_llm_cost_usd_total") == 0.0
    )  # no prices configured: zero, not a guess


def test_blocked_tool_calls_are_counted_per_agent(metrics: Metrics) -> None:
    audit = AuditLog("run1")
    audit.record("log_analyst", "get_job_info", {}, "ok")
    audit.record("code_investigator", "commit_files", {}, "blocked", "not allowed")
    assert value(metrics, "pd_tool_calls_total", agent="log_analyst", status="ok") == 1
    assert value(metrics, "pd_guardrail_blocks_total", agent="code_investigator") == 1


# ---- end to end: queue -> investigation -> ticket ----------------------------------------------


class World:
    def __init__(self, settings: Settings, redis: Any, scripts: dict[str, Any], **kw: Any) -> None:
        self.jira, self.github = JiraMock.load(FIXTURES), GitHubMock.load(FIXTURES)
        self.llm = ScriptedLlm(scripts=scripts)
        settings = replace(settings, **kw)
        investigator = Investigator(settings, small_model=self.llm, large_model=self.llm)
        tickets = TicketService(
            settings,
            jira=lambda: Client(jira_server(self.jira)),
            github=lambda: Client(github_server(self.github)),
        )
        self.consumer = StreamConsumer(
            redis,
            stream=STREAM,
            group=GROUP,
            consumer="w1",
            handler=EventHandler(redis, investigator, tickets),
            max_attempts=3,
            min_idle_ms=20,
            block_ms=10,
        )


async def run_event(redis: Any, world: World, event: dict[str, Any], copies: int = 1) -> None:
    await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    for _ in range(copies):
        await redis.xadd(STREAM, {"data": json.dumps(event)})
    for _ in range(8):
        await world.consumer.poll_once()
        await asyncio.sleep(0.03)


async def test_a_run_is_counted_end_to_end(
    redis: Any, settings: Settings, metrics: Metrics, spans: InMemorySpanExporter
) -> None:
    world = World(settings, redis, logic_bug_scripts())
    await run_event(redis, world, EVENT, copies=2)  # the second one is a duplicate delivery

    assert value(metrics, "pd_events_total", result="processed") == 1
    assert value(metrics, "pd_events_total", result="duplicate") == 1
    assert value(metrics, "pd_investigations_total", outcome="ok") == 1
    assert value(metrics, "pd_diagnoses_total", category="code", action="fix") == 1
    assert value(metrics, "pd_tickets_total", action="created", degraded="false") == 1
    assert value(metrics, "pd_llm_tokens_total", kind="prompt") > 0
    assert value(metrics, "pd_llm_cost_usd_total") > 0
    assert value(metrics, "pd_investigation_steps_count") == 1
    assert value(metrics, "pd_tool_calls_total", agent="log_analyst", status="ok") >= 1
    # queue gauges were sampled after the poll
    assert value(metrics, "pd_queue_pending_messages") == 0
    assert value(metrics, "pd_dlq_messages") == 0
    assert value(metrics, "pd_worker_last_poll_timestamp_seconds") > 0


async def test_failures_budget_trips_and_the_dlq_show_up(
    redis: Any, settings: Settings, metrics: Metrics
) -> None:
    steps = {r: [reply("x")] * 5 for r in ("log_analyst", "code_investigator", "diagnose_agent")}
    world = World(settings, redis, steps, token_budget=1)
    await run_event(redis, world, {k: v for k, v in EVENT.items() if k != "traceparent"})

    assert value(metrics, "pd_investigations_total", outcome="budget_exceeded") == 3  # 3 attempts
    assert value(metrics, "pd_budget_trips_total", limit="token_budget") == 3
    assert value(metrics, "pd_events_total", result="failed") == 2  # attempts 1 and 2 retried
    assert value(metrics, "pd_tickets_total", action="created", degraded="true") == 1


async def test_a_dead_lettered_message_is_visible_in_the_gauge(
    redis: Any, metrics: Metrics
) -> None:
    from pipeline_doctor_agent.worker.consumer import PermanentError

    async def bad(_: Delivery) -> None:
        raise PermanentError("never works")

    c = StreamConsumer(
        redis, stream=STREAM, group=GROUP, consumer="w", handler=bad, min_idle_ms=20, block_ms=10
    )
    await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    await redis.xadd(STREAM, {"data": "x"})
    await c.poll_once()
    assert value(metrics, "pd_events_total", result="dead_lettered") == 1
    assert value(metrics, "pd_dlq_messages") == 1


# ---- traces -------------------------------------------------------------------------------------


def by_name(spans: list[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name == name]


async def test_one_trace_continues_the_ingress_trace_through_every_step(
    redis: Any, settings: Settings, spans: InMemorySpanExporter
) -> None:
    await run_event(redis, World(settings, redis, logic_bug_scripts()), EVENT)
    finished = list(spans.get_finished_spans())

    (event_span,) = by_name(finished, "pipeline_doctor.event")
    assert format(event_span.context.trace_id, "032x") == TRACE_ID  # the ingress' trace id
    assert event_span.parent is not None and format(event_span.parent.span_id, "016x") == "b" * 16
    for step in ("pipeline_doctor.investigate", "pipeline_doctor.ticket"):
        (s,) = by_name(finished, step)
        assert format(s.context.trace_id, "032x") == TRACE_ID
    # ADK's own agent / model / tool spans hang off the same trace
    adk = [
        s
        for s in finished
        if s.name.startswith(("invoke_agent", "generate_content", "execute_tool"))
    ]
    assert adk, [s.name for s in finished]
    assert all(format(s.context.trace_id, "032x") == TRACE_ID for s in adk)


async def test_spans_never_carry_build_logs_prompts_or_model_answers(
    redis: Any, settings: Settings, spans: InMemorySpanExporter
) -> None:
    assert os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] == "false"
    await run_event(redis, World(settings, redis, logic_bug_scripts()), EVENT)
    every_value = " ".join(
        str(v) for s in spans.get_finished_spans() for v in (s.attributes or {}).values()
    )
    # text from the log, from the model's answer, and from the prompt must not reach a trace backend
    for forbidden in ("assert 10.0 == 90.0", "price * rate", "ROLE:", "get_failed_log_excerpt("):
        assert forbidden not in every_value


@pytest.mark.parametrize("capture", ["default", "true"])
async def test_the_content_switch_is_what_keeps_prompts_out_of_legacy_spans(
    redis: Any,
    settings: Settings,
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    capture: str,
) -> None:
    """ADK's legacy span schema writes tool arguments and results onto spans unless the content
    switch is off. The ``true`` case proves this test can see a leak; ``default`` is what we ship."""
    monkeypatch.setenv("ADK_TELEMETRY_SCHEMA_VERSION_OPT_IN", "1")
    if capture == "true":
        monkeypatch.setenv("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "true")
    await run_event(redis, World(settings, redis, logic_bug_scripts()), EVENT)
    every_value = " ".join(
        str(v) for s in spans.get_finished_spans() for v in (s.attributes or {}).values()
    )
    leaked = re.search(r'max_lines"\s*:\s*20', every_value) is not None  # a value the model passed
    assert leaked is (capture == "true")


def test_a_failing_span_records_the_error_type_not_its_message(spans: InMemorySpanExporter) -> None:
    with pytest.raises(RuntimeError), span("pipeline_doctor.test"):
        raise RuntimeError("password=hunter2 leaked from a build log")
    (s,) = spans.get_finished_spans()
    assert s.status.status_code == StatusCode.ERROR and s.status.description == "RuntimeError"
    assert "hunter2" not in json.dumps(dict(s.attributes or {})) + str(s.events)


@pytest.mark.parametrize("bad", [None, "", "garbage", "00-" + "0" * 32 + "-" + "0" * 16 + "-01"])
def test_a_missing_or_invalid_traceparent_starts_a_fresh_trace(bad: str | None) -> None:
    assert context_from_traceparent(bad) is None


# ---- logs ---------------------------------------------------------------------------------------


def test_logs_are_json_with_extras_and_the_trace_id(spans: InMemorySpanExporter) -> None:
    record_args = ("pipeline_doctor.x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    rec = logging.LogRecord(*record_args)
    rec.run_id = "abc"
    rec.big = "z" * 5000
    with span("pipeline_doctor.logs") as sp:
        line = json.loads(JsonFormatter().format(rec))
        expected = format(sp.get_span_context().trace_id, "032x")
    assert line["msg"] == "hello world" and line["run_id"] == "abc"
    assert line["trace_id"] == expected and len(line["big"]) == 500
    assert "trace_id" not in json.loads(JsonFormatter().format(rec))  # outside a span: no ids


# ---- the /metrics endpoint ------------------------------------------------------------------------


def test_metrics_endpoint_serves_the_registry_on_loopback(metrics: Metrics) -> None:
    metrics.event("processed")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server, _thread = start_metrics_server(metrics, addr="127.0.0.1", port=port)
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
    finally:
        server.shutdown()
    assert 'pd_events_total{result="processed"} 1.0' in body


def test_port_zero_disables_the_endpoint(metrics: Metrics) -> None:
    assert start_metrics_server(metrics, addr="127.0.0.1", port=0) is None


def test_settings_read_the_observability_variables() -> None:
    s = Settings.from_env(
        {
            "METRICS_PORT": "9999",
            "METRICS_ADDR": "0.0.0.0",  # noqa: S104
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://jaeger:4317",
            "PRICE_IN_PER_MTOK": "0.3",
            "PRICE_OUT_PER_MTOK": "2.5",
        }
    )
    assert (s.metrics_port, s.otlp_endpoint, s.price_out_per_mtok) == (
        9999,
        "http://jaeger:4317",
        2.5,
    )
    assert (
        Settings.from_env({}).otlp_endpoint is None
        and Settings.from_env({}).metrics_addr == "127.0.0.1"
    )
    with pytest.raises(ValueError, match="METRICS_PORT"):
        Settings.from_env({"METRICS_PORT": "70000"})
    with pytest.raises(ValueError, match="negative"):
        Settings.from_env({"PRICE_IN_PER_MTOK": "-1"})


# ---- dashboard and alert files stay in step with the code ----------------------------------------

OBS = ROOT / "deploy" / "observability"


def exported_metric_names() -> set[str]:
    """Every series name the worker can export, plus the ingress' (read from its Go source)."""
    names: set[str] = set()
    for fam in Metrics().registry.collect():
        if fam.type == "counter":
            names.add(fam.name + "_total")
        elif fam.type == "histogram":
            names |= {fam.name + s for s in ("_bucket", "_sum", "_count")}
        else:
            names.add(fam.name)
    go = (ROOT / "services/ingress/internal/server/metrics.go").read_text()
    for n in re.findall(r'Name:\s*"([a-z_]+)"', go):
        names |= {n + s for s in ("_bucket", "_sum", "_count")} if n.endswith("_seconds") else {n}
    return names


def referenced_metrics(expr: str) -> set[str]:
    return set(re.findall(r"\b((?:pd|ingress)_[a-z_]+)\b", expr))


def all_rules() -> list[dict[str, Any]]:
    doc = yaml.safe_load((OBS / "alerts.yml").read_text())
    return [r for g in doc["groups"] for r in g["rules"]]


def test_every_alert_is_complete_and_uses_metrics_that_exist() -> None:
    known = exported_metric_names()
    rules = all_rules()
    assert len(rules) >= 10
    names = [r["alert"] for r in rules]
    assert len(names) == len(set(names))
    for r in rules:
        assert r["labels"]["severity"] in {"page", "ticket"}, r["alert"]
        assert r["annotations"]["summary"] and r["annotations"]["description"], r["alert"]
        used = referenced_metrics(r["expr"])
        assert used or r["expr"].startswith("up{"), r["alert"]  # `up` is built in
        assert used <= known, (r["alert"], used - known)


def test_the_required_alerts_exist() -> None:
    names = {r["alert"] for r in all_rules()}
    assert {
        "PipelineDoctorDlqNotEmpty",  # the spec's "DLQ not empty"
        "PipelineDoctorTokenSpike",  # "cost spike"
        "PipelineDoctorDailyCostHigh",
        "PipelineDoctorRunLimitsHit",  # "step-limit hits"
        "PipelineDoctorGuardrailBlocks",
        "PipelineDoctorWorkerStalled",
    } <= names


def test_the_dashboard_is_valid_and_queries_only_real_metrics() -> None:
    dash = json.loads((OBS / "grafana/dashboards/pipeline-doctor.json").read_text())
    known = exported_metric_names()
    panels = [p for p in dash["panels"] if p["type"] != "row"]
    assert len(panels) >= 20 and dash["uid"] == "pipeline-doctor"
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids))
    for p in panels:
        assert p["title"] and p["targets"], p
        for t in p["targets"]:
            used = referenced_metrics(t["expr"])
            assert used and used <= known, (p["title"], used - known)
        assert p["datasource"]["uid"] == "prometheus"


def test_prometheus_config_loads_the_rules_and_scrapes_both_services() -> None:
    cfg = yaml.safe_load((OBS / "prometheus.yml").read_text())
    assert {j["job_name"] for j in cfg["scrape_configs"]} == {"ingress", "pipeline-doctor-worker"}
    assert cfg["rule_files"] == ["/etc/prometheus/alerts.yml"]
    for rule in all_rules():  # the job names used in alert expressions are real jobs
        for job in re.findall(r'job="([^"]+)"', rule["expr"]):
            assert job in {"ingress", "pipeline-doctor-worker"}


def test_every_label_used_in_alerts_and_dashboard_is_one_the_code_emits() -> None:
    """e.g. outcome!="ok", degraded="true", status=~"failed|error" must match real label values."""
    from pipeline_doctor_agent.observability import metrics as m

    allowed = {
        "outcome": m.INVESTIGATION_OUTCOMES,
        "status": m.FIX_STATUSES | m.TOOL_STATUSES,
        "result": m.EVENT_RESULTS,
        "degraded": {"true", "false"},
        "limit": m.BUDGET_LIMITS,
    }
    text = (
        " ".join(r["expr"] for r in all_rules())
        + (OBS / "grafana/dashboards/pipeline-doctor.json").read_text()
    )
    for label, values in allowed.items():
        for match in re.finditer(rf'\b{label}(?:=|=~)\\?"([^"\\]+)\\?"', text):
            for v in match.group(1).split("|"):
                assert v in values, (label, v)
