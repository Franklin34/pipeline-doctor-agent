"""OpenTelemetry tracing: one trace per failure, from the webhook to the ticket.

The Go ingress writes a W3C ``traceparent`` into every queued event. The worker continues that
trace, so one trace id links the webhook, the queue wait, the diagnosis, the fix and the ticket.
ADK's own spans (agent runs, model calls, tool calls) nest under our spans automatically because
they use the same global tracer provider.

Spans carry identifiers and counts, never content. ADK records prompts, tool arguments and tool
results on its spans by default; ``setup_tracing`` turns that off, because those can hold build
logs and code that a trace backend (often a third party) should not receive.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

TRACER_NAME = "pipeline_doctor"
_propagator = TraceContextTextMapPropagator()


def setup_tracing(
    *,
    service_name: str = "pipeline-doctor-worker",
    otlp_endpoint: str | None = None,
    extra_processors: list[SpanProcessor] | None = None,
    exporter: SpanExporter | None = None,
) -> TracerProvider:
    """Install the global tracer provider. With no endpoint nothing leaves the process, but spans
    still get valid ids, so logs can carry a trace id either way."""
    # Must be set before ADK reads it; setdefault keeps an operator's explicit choice.
    os.environ.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false")
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is None and otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(
            endpoint=otlp_endpoint, insecure=otlp_endpoint.startswith("http://")
        )
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    for processor in extra_processors or []:
        provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    return provider


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def context_from_traceparent(traceparent: str | None) -> otel_context.Context | None:
    """The remote parent from the event's ``traceparent``. A missing or malformed value gives None
    (a fresh trace), never an error: telemetry must not be able to fail a run."""
    if not traceparent:
        return None
    ctx = _propagator.extract({"traceparent": traceparent})
    return ctx if trace.get_current_span(ctx).get_span_context().is_valid else None


@contextmanager
def span(
    name: str,
    *,
    parent: otel_context.Context | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: dict[str, Any] | None = None,
) -> Iterator[trace.Span]:
    """A span that is current inside the block. An exception marks it as an error using only the
    exception TYPE: messages can contain text from builds, so they stay out of traces."""
    with tracer().start_as_current_span(
        name,
        context=parent,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as s:
        try:
            yield s
        except BaseException as exc:
            s.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            s.set_attribute("error.type", type(exc).__name__)
            raise


def current_ids() -> tuple[str, str] | None:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
