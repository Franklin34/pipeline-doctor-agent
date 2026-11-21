"""The worker's /metrics endpoint (Prometheus scrapes it)."""

from __future__ import annotations

from typing import Any

from prometheus_client import start_http_server

from pipeline_doctor_agent.observability.metrics import Metrics


def start_metrics_server(metrics: Metrics, *, addr: str, port: int) -> Any:
    """Serve /metrics on ``addr:port`` from a background thread. Port 0 turns it off.

    The default address is loopback: metrics describe internal behaviour and are not for the open
    network. Inside a container set ``METRICS_ADDR=0.0.0.0`` and publish the port deliberately.
    """
    if port == 0:
        return None
    return start_http_server(port, addr=addr, registry=metrics.registry)
