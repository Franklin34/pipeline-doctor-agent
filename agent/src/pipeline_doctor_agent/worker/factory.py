"""Pick the queue transport from settings."""

from __future__ import annotations

from redis.asyncio import Redis

from pipeline_doctor_agent.settings import Settings
from pipeline_doctor_agent.worker.consumer import Handler, StreamConsumer
from pipeline_doctor_agent.worker.pubsub_consumer import PubSubConsumer


def build_consumer(
    settings: Settings, redis: Redis, handler: Handler, *, lease_s: int
) -> StreamConsumer | PubSubConsumer:
    if settings.queue_backend == "pubsub":
        # The ack deadline is extended while a message is being worked on, so it does not have to
        # cover a whole diagnosis plus fix; it only bounds how fast a crashed worker's message returns.
        return PubSubConsumer(
            project=settings.pubsub_project,
            subscription=settings.pubsub_subscription,
            dead_letter_topic=settings.pubsub_dead_letter_topic,
            handler=handler,
            max_attempts=settings.max_attempts,
            ack_deadline_s=settings.pubsub_ack_deadline_s,
        )
    return StreamConsumer(
        redis,
        stream=settings.stream,
        group=settings.group,
        consumer=settings.consumer,
        handler=handler,
        max_attempts=settings.max_attempts,
        min_idle_ms=max(settings.min_idle_ms, lease_s * 1000),
        block_ms=settings.block_ms,
    )
