"""Prometheus metrics for the worker.

Rules that keep this safe to run for years:

* **Bounded labels only.** Every label value comes from a small fixed set (an enum, an agent name,
  an error kind). Never a job name, build number, fingerprint, issue key or any text from a log:
  those make the number of time series grow without limit and take Prometheus down.
* **No content.** Metrics carry counts and durations, never anything a build or a model wrote.
* A ``Metrics`` object owns its own registry, so tests start from zero and never share state.
"""

from __future__ import annotations

from collections.abc import Collection

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from pipeline_doctor_agent.screening import KINDS

# Closed vocabularies. Anything outside them is folded into "other" so a new value can never create
# a new time series by accident.
INVESTIGATION_OUTCOMES = {
    "ok",
    "facts_unavailable",
    "timeout",
    "budget_exceeded",
    "agent_error",
    "invalid_output",
}
BUDGET_LIMITS = {"max_llm_calls", "max_tool_calls", "token_budget"}
FIX_STATUSES = {"pr_opened", "reused_pr", "skipped", "failed", "error"}
TICKET_ACTIONS = {"created", "commented", "comment_already_present"}
EVENT_RESULTS = {"processed", "duplicate", "failed", "dead_lettered"}
TOOL_STATUSES = {"ok", "error", "blocked"}
AGENTS = {
    "log_analyst",
    "code_investigator",
    "knowledge_agent",
    "diagnose_agent",
    "fix_agent",
    "pr_publisher",
    "ticket_agent",
}
CATEGORIES = {
    "code",
    "test",
    "dependency",
    "infrastructure",
    "flaky",
    "configuration",
    "unknown",
}
ACTIONS = {"fix", "ticket_only"}


def _bounded(value: object, allowed: Collection[str]) -> str:
    text = str(getattr(value, "value", value))
    return text if text in allowed else "other"


class Metrics:
    def __init__(
        self,
        registry: CollectorRegistry | None = None,
        *,
        price_in_per_mtok: float = 0.0,
        price_out_per_mtok: float = 0.0,
    ) -> None:
        self.registry = registry or CollectorRegistry()
        self.price_in, self.price_out = price_in_per_mtok, price_out_per_mtok
        r = self.registry
        self.events = Counter(
            "pd_events_total", "Queue messages by handling result.", ["result"], registry=r
        )
        self.investigations = Counter(
            "pd_investigations_total",
            "Investigation runs by outcome (ok or the kind of failure).",
            ["outcome"],
            registry=r,
        )
        self.diagnoses = Counter(
            "pd_diagnoses_total",
            "Diagnoses by category and the action the policy gate chose.",
            ["category", "action"],
            registry=r,
        )
        self.duration = Histogram(
            "pd_investigation_duration_seconds",
            "Wall-clock time of an investigation.",
            buckets=(1, 2.5, 5, 10, 20, 40, 80, 160, 300),
            registry=r,
        )
        self.steps = Histogram(
            "pd_investigation_steps",
            "Model calls plus tool calls in one investigation.",
            buckets=(2, 4, 6, 8, 12, 16, 24, 32, 48, 90),
            registry=r,
        )
        self.tokens = Counter("pd_llm_tokens_total", "Model tokens used.", ["kind"], registry=r)
        self.cost = Counter(
            "pd_llm_cost_usd_total",
            "Estimated model spend in USD (from the configured per-million-token prices).",
            registry=r,
        )
        self.budget_trips = Counter(
            "pd_budget_trips_total", "Runs stopped by a run limit.", ["limit"], registry=r
        )
        self.tool_calls = Counter(
            "pd_tool_calls_total",
            "Tool calls by agent and result.",
            ["agent", "status"],
            registry=r,
        )
        self.guardrail_blocks = Counter(
            "pd_guardrail_blocks_total",
            "Tool calls refused by the policy layer (a model asked for something it may not do).",
            ["agent"],
            registry=r,
        )
        self.injection_flags = Counter(
            "pd_injection_flags_total",
            "Runs where input screening saw text that tries to instruct the model.",
            ["kind"],
            registry=r,
        )
        self.fixes = Counter(
            "pd_fix_attempts_total", "Automatic fix attempts by outcome.", ["status"], registry=r
        )
        self.fix_duration = Histogram(
            "pd_fix_duration_seconds",
            "Wall-clock time of a fix attempt.",
            buckets=(5, 15, 30, 60, 120, 240, 480, 600),
            registry=r,
        )
        self.tickets = Counter(
            "pd_tickets_total",
            "Ticket writes by action; degraded means the model was unavailable.",
            ["action", "degraded"],
            registry=r,
        )
        # Queue health, sampled by the worker.
        self.queue_pending = Gauge(
            "pd_queue_pending_messages", "Delivered but not yet acknowledged.", registry=r
        )
        self.queue_lag = Gauge(
            "pd_queue_lag_messages", "Waiting in the stream, not yet delivered.", registry=r
        )
        self.dlq_size = Gauge("pd_dlq_messages", "Messages in the dead-letter stream.", registry=r)
        self.last_poll = Gauge(
            "pd_worker_last_poll_timestamp_seconds",
            "Unix time of the last completed queue poll (liveness).",
            registry=r,
        )

    # -- recording helpers: one call per fact, so call sites stay one line ---------------------
    def event(self, result: str) -> None:
        self.events.labels(_bounded(result, EVENT_RESULTS)).inc()

    def investigation(self, outcome: str) -> None:
        self.investigations.labels(_bounded(outcome, INVESTIGATION_OUTCOMES)).inc()

    def diagnosis(self, category: object, action: object) -> None:
        self.diagnoses.labels(_bounded(category, CATEGORIES), _bounded(action, ACTIONS)).inc()

    def usage(self, *, prompt: int, output: int, steps: int, duration_s: float) -> None:
        self.tokens.labels("prompt").inc(prompt)
        self.tokens.labels("output").inc(output)
        self.cost.inc(prompt / 1e6 * self.price_in + output / 1e6 * self.price_out)
        self.steps.observe(steps)
        self.duration.observe(duration_s)

    def budget_trip(self, limit: str) -> None:
        self.budget_trips.labels(_bounded(limit, BUDGET_LIMITS)).inc()

    def tool_call(self, agent: str, status: str) -> None:
        self.tool_calls.labels(_bounded(agent, AGENTS), _bounded(status, TOOL_STATUSES)).inc()

    def guardrail_block(self, agent: str) -> None:
        self.guardrail_blocks.labels(_bounded(agent, AGENTS)).inc()

    def injection_flag(self, kind: str) -> None:
        self.injection_flags.labels(_bounded(kind, KINDS)).inc()

    def fix(self, status: str, duration_s: float) -> None:
        self.fixes.labels(_bounded(status, FIX_STATUSES)).inc()
        self.fix_duration.observe(duration_s)

    def ticket(self, action: str, degraded: bool) -> None:
        self.tickets.labels(_bounded(action, TICKET_ACTIONS), "true" if degraded else "false").inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)


# The process-wide instance. Tests replace it with ``install``.
_current = Metrics()


def current() -> Metrics:
    return _current


def install(metrics: Metrics) -> Metrics:
    global _current
    _current = metrics
    return metrics
