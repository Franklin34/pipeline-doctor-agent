"""Redis Streams consumer, same protocol as the Go adapter (docs/ingress.md).

Unacknowledged messages stay pending; ``XAUTOCLAIM`` redelivers them after ``min_idle_ms``;
Redis' own delivery counter bounds retries; at ``max_attempts`` the message moves to the
``<stream>-dlq`` stream with the last error. Delivery is at-least-once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from pipeline_doctor_agent.observability import metrics

log = logging.getLogger("pipeline_doctor.consumer")


@dataclass(frozen=True)
class Delivery:
    """One delivery of a message. ``attempt`` is Redis' delivery count (1 on the first try)."""

    body: str
    attempt: int
    max_attempts: int

    @property
    def is_final(self) -> bool:
        """True when a failure now sends the message to the dead-letter queue."""
        return self.attempt >= self.max_attempts


Handler = Callable[[Delivery], Awaitable[None]]


class PermanentError(Exception):
    """The message can never succeed (for example a malformed payload): dead-letter it now."""


class StreamConsumer:
    def __init__(
        self,
        redis: Redis,
        *,
        stream: str,
        group: str,
        consumer: str,
        handler: Handler,
        max_attempts: int = 3,
        min_idle_ms: int = 60_000,
        block_ms: int = 2_000,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.r, self.stream, self.group, self.consumer = redis, stream, group, consumer
        self.dlq = f"{stream}-dlq"
        self.handler, self.max_attempts = handler, max_attempts
        self.min_idle_ms, self.block_ms = min_idle_ms, block_ms

    async def run(self, stop: asyncio.Event) -> None:
        try:
            await self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        while not stop.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("queue poll failed; backing off")
                await _sleep(stop, self.block_ms / 1000)

    async def poll_once(self) -> int:
        """Reclaimed messages first (they waited longest), then new ones. Returns messages handled."""
        handled = 0
        claimed: Any = await self.r.xautoclaim(
            self.stream,
            self.group,
            self.consumer,
            min_idle_time=self.min_idle_ms,
            start_id="0-0",
            count=10,
        )
        for msg_id, fields in claimed[1]:
            if not fields:  # entry was trimmed from the stream
                await self.r.xack(self.stream, self.group, msg_id)
                continue
            attempts = await self._attempts(msg_id)
            if attempts > self.max_attempts:
                await self._dead_letter(
                    msg_id, fields, attempts, "exceeded max attempts (worker may have crashed)"
                )
            else:
                await self._handle(msg_id, fields, attempts)
            handled += 1
        new: Any = await self.r.xreadgroup(
            self.group, self.consumer, {self.stream: ">"}, count=10, block=self.block_ms
        )
        for _stream, entries in new or []:
            for msg_id, fields in entries:
                await self._handle(msg_id, fields, 1)
                handled += 1
        await self._sample()
        return handled

    async def _handle(self, msg_id: str, fields: dict[str, Any], attempt: int) -> None:
        try:
            await self.handler(Delivery(fields.get("data", ""), attempt, self.max_attempts))
        except PermanentError as exc:
            await self._dead_letter(msg_id, fields, attempt, f"permanent: {exc}")
        except Exception as exc:
            metrics.current().event("failed")
            # The traceback goes to the log; the DLQ entry keeps only a short, scrubbed-by-size reason.
            log.warning(
                "handler failed",
                exc_info=True,
                extra={"id": msg_id, "attempt": attempt, "final": attempt >= self.max_attempts},
            )
            if attempt >= self.max_attempts:
                await self._dead_letter(msg_id, fields, attempt, f"{type(exc).__name__}: {exc}")
        else:
            await self.r.xack(self.stream, self.group, msg_id)

    async def _sample(self) -> None:
        """Queue depth, backlog age signals and DLQ size for the gauges. Telemetry must never be able
        to stop the worker, so any error here is swallowed after a debug log."""
        m = metrics.current()
        try:
            for group in await self.r.xinfo_groups(self.stream):
                if _text(group.get("name")) == self.group:
                    m.queue_pending.set(int(group.get("pending") or 0))
                    m.queue_lag.set(int(group.get("lag") or 0))
            m.dlq_size.set(await self.r.xlen(self.dlq))
            m.last_poll.set(time.time())
        except Exception:
            log.debug("queue sampling failed", exc_info=True)

    async def _attempts(self, msg_id: str) -> int:
        rows = await self.r.xpending_range(self.stream, self.group, min=msg_id, max=msg_id, count=1)
        if not rows:
            raise RuntimeError(f"message {msg_id} is not pending")
        return int(rows[0]["times_delivered"])

    async def _dead_letter(
        self, msg_id: str, fields: dict[str, Any], attempts: int, reason: str
    ) -> None:
        await self.r.xadd(
            self.dlq,
            {
                "data": fields.get("data", ""),
                "source_id": msg_id,
                "attempts": str(attempts),
                "error": reason[:500],
                "failed_at": str(int(time.time())),
            },
        )
        await self.r.xack(self.stream, self.group, msg_id)
        metrics.current().event("dead_lettered")
        log.error(
            "message moved to DLQ",
            extra={"id": msg_id, "attempts": attempts, "reason": reason[:200]},
        )


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
