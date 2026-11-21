"""Run the worker: ``python -m pipeline_doctor_agent.worker``."""

from __future__ import annotations

import asyncio
import logging
import signal

from redis.asyncio import Redis

from pipeline_doctor_agent.fix.factory import build_fix_service
from pipeline_doctor_agent.investigator import Investigator
from pipeline_doctor_agent.knowledge import build_searcher
from pipeline_doctor_agent.observability import Metrics, configure_logging, install, setup_tracing
from pipeline_doctor_agent.observability.server import start_metrics_server
from pipeline_doctor_agent.settings import Settings
from pipeline_doctor_agent.ticket import TicketService
from pipeline_doctor_agent.worker.consumer import StreamConsumer
from pipeline_doctor_agent.worker.handler import EventHandler


async def main() -> None:
    configure_logging(logging.INFO)
    settings = Settings.from_env()
    setup_tracing(otlp_endpoint=settings.otlp_endpoint)
    metrics = install(
        Metrics(
            price_in_per_mtok=settings.price_in_per_mtok,
            price_out_per_mtok=settings.price_out_per_mtok,
        )
    )
    start_metrics_server(metrics, addr=settings.metrics_addr, port=settings.metrics_port)
    fixer = build_fix_service(settings)
    # A message is "being worked on" for as long as a diagnosis plus a fix can take. The lease and the
    # queue's reclaim timeout must both outlast that, or a second worker would pick up the same event.
    lease_s = int(settings.run_timeout_s + (settings.fix_timeout_s if fixer else 0) + 120)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    consumer = StreamConsumer(
        redis,
        stream=settings.stream,
        group=settings.group,
        consumer=settings.consumer,
        handler=EventHandler(
            redis,
            Investigator(settings, searcher=build_searcher(settings)),
            TicketService(settings),
            fixer=fixer,
            lease_s=lease_s,
        ),
        max_attempts=settings.max_attempts,
        min_idle_ms=max(settings.min_idle_ms, lease_s * 1000),
        block_ms=settings.block_ms,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)  # finish the current message, then exit
    try:
        await consumer.run(stop)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
