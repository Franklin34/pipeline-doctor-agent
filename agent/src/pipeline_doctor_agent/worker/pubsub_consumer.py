"""Google Cloud Pub/Sub consumer: the cloud counterpart of ``StreamConsumer``.

Same contract, different machinery. What Redis Streams made us write, Pub/Sub does for us, so this
class is mostly about *not* getting it wrong:

* **Retries and the dead-letter topic belong to the subscription** (``max_delivery_attempts``,
  retry backoff, ``dead_letter_policy``, all in Terraform). A handler that raises simply does not
  acknowledge; Pub/Sub redelivers with backoff and, after the configured attempts, forwards the
  message to the dead-letter topic. ``max_attempts`` here MUST equal the subscription's value: it
  only decides which delivery is "final" (so the fallback ticket is written).
* **A malformed event (``PermanentError``) is dead-lettered at once** by publishing it to the
  dead-letter topic ourselves and then acknowledging, because retrying can never help.
* **Long work keeps its lease.** A diagnosis plus a fix can outlast any ack deadline, so while the
  handler runs a background task keeps extending the deadline; if the worker dies, extensions stop
  and Pub/Sub redelivers.
* One message at a time per worker: leases and ordering stay simple; scale by adding workers.

The Pub/Sub client is synchronous, so its calls run in threads.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from google.api_core import exceptions as gexc

from pipeline_doctor_agent.observability import metrics
from pipeline_doctor_agent.worker.consumer import Delivery, Handler, PermanentError

log = logging.getLogger("pipeline_doctor.pubsub")
MIN_DEAD_LETTER_ATTEMPTS = 5  # Pub/Sub's own lower bound for a dead-letter policy
MAX_DEAD_LETTER_ATTEMPTS = 100


class PubSubConsumer:
    def __init__(
        self,
        *,
        project: str,
        subscription: str,
        dead_letter_topic: str,
        handler: Handler,
        max_attempts: int = 5,
        ack_deadline_s: int = 60,
        pull_timeout_s: float = 10.0,
        subscriber: Any = None,
        publisher: Any = None,
    ) -> None:
        if not MIN_DEAD_LETTER_ATTEMPTS <= max_attempts <= MAX_DEAD_LETTER_ATTEMPTS:
            raise ValueError(
                f"max_attempts must be {MIN_DEAD_LETTER_ATTEMPTS}-{MAX_DEAD_LETTER_ATTEMPTS} "
                "for Pub/Sub, and equal the subscription's max_delivery_attempts"
            )
        if subscriber is None or publisher is None:
            from google.cloud import pubsub_v1  # type: ignore[import-untyped]

            subscriber = subscriber or pubsub_v1.SubscriberClient()
            publisher = publisher or pubsub_v1.PublisherClient()
        self.sub, self.pub = subscriber, publisher
        self.subscription = self.sub.subscription_path(project, subscription)
        self.dlq_topic = self.pub.topic_path(project, dead_letter_topic)
        self.handler, self.max_attempts = handler, max_attempts
        self.ack_deadline_s, self.pull_timeout_s = ack_deadline_s, pull_timeout_s

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                if await self.poll_once() == 0:
                    await _sleep(stop, 1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pubsub poll failed; backing off")
                await _sleep(stop, 5.0)

    async def poll_once(self) -> int:
        """Pull and handle at most one message. Returns the number handled (0 or 1)."""
        try:
            response = await asyncio.to_thread(
                self.sub.pull,
                request={"subscription": self.subscription, "max_messages": 1},
                timeout=self.pull_timeout_s,
            )
        except (gexc.DeadlineExceeded, gexc.ServiceUnavailable):  # nothing to do, or a blip
            metrics.current().last_poll.set(time.time())
            return 0
        metrics.current().last_poll.set(time.time())
        received = list(response.received_messages)
        for item in received:
            await self._handle(item)
        return len(received)

    async def _handle(self, item: Any) -> None:
        ack_id = item.ack_id
        data = bytes(item.message.data).decode("utf-8", errors="replace")
        attempt = int(
            getattr(item, "delivery_attempt", 0) or 1
        )  # 0: no dead-letter policy attached
        extender = asyncio.create_task(self._extend(ack_id))
        try:
            await self.handler(Delivery(data, attempt, self.max_attempts))
        except PermanentError as exc:
            await self._dead_letter(item, data, attempt, f"permanent: {exc}")
        except Exception as exc:
            metrics.current().event("failed")
            final = attempt >= self.max_attempts
            log.warning(
                "handler failed",
                exc_info=True,
                extra={"id": item.message.message_id, "attempt": attempt, "final": final},
            )
            if final:  # Pub/Sub forwards it to the dead-letter topic after this delivery
                metrics.current().event("dead_lettered")
                log.error(
                    "message will be moved to the DLQ by the subscription",
                    extra={"id": item.message.message_id, "reason": f"{type(exc).__name__}"},
                )
            await self._nack(ack_id)
        else:
            await asyncio.to_thread(
                self.sub.acknowledge,
                request={"subscription": self.subscription, "ack_ids": [ack_id]},
            )
        finally:
            extender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await extender

    async def _extend(self, ack_id: str) -> None:
        """Keep the lease while the handler works; stops when the handler finishes or the task dies."""
        interval = max(1.0, self.ack_deadline_s / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(
                    self.sub.modify_ack_deadline,
                    request={
                        "subscription": self.subscription,
                        "ack_ids": [ack_id],
                        "ack_deadline_seconds": self.ack_deadline_s,
                    },
                )
            except (
                Exception
            ):  # a failed extension means a possible redelivery; the handler is idempotent
                log.warning("could not extend the ack deadline", exc_info=True)

    async def _nack(self, ack_id: str) -> None:
        await asyncio.to_thread(
            self.sub.modify_ack_deadline,
            request={
                "subscription": self.subscription,
                "ack_ids": [ack_id],
                "ack_deadline_seconds": 0,
            },
        )

    async def _dead_letter(self, item: Any, data: str, attempt: int, reason: str) -> None:
        try:
            future = self.pub.publish(
                self.dlq_topic,
                data.encode("utf-8"),
                source_id=str(item.message.message_id),
                attempts=str(attempt),
                error=reason[:500],
                failed_at=str(int(time.time())),
            )
            await asyncio.to_thread(future.result, 30)
        except Exception:
            log.exception("could not publish to the dead-letter topic; leaving the message")
            await self._nack(item.ack_id)  # retried, so it is not lost
            return
        await asyncio.to_thread(
            self.sub.acknowledge,
            request={"subscription": self.subscription, "ack_ids": [item.ack_id]},
        )
        metrics.current().event("dead_lettered")
        log.error(
            "message moved to DLQ", extra={"id": item.message.message_id, "reason": reason[:200]}
        )


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
