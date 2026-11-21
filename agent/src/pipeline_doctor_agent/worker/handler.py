"""Turns a queue message into an investigation and a ticket, idempotently.

The queue delivers at-least-once, so the same event can arrive twice (ingress retries, a redelivery
after a crash). Redis keys keep every external effect single:

  done:<key>        the whole run finished; later deliveries are acknowledged and skipped
  lease:<key>       a worker is on it right now; others back off and retry later
  ticket:<key>      the ticket step finished (its outcome); a redelivery skips straight to "done"
  ticketlock:<fp>   one ticket write per failure fingerprint at a time, so two events for the same
                    failure cannot both create a ticket

If a worker dies between writing the ticket and recording ``ticket:<key>``, the redelivered run
finds the ticket by fingerprint and its comment carries an idempotency key, so nothing is
duplicated. If the model is unavailable on the FINAL attempt, a basic ticket is written instead
of letting the failure vanish into the dead-letter queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Protocol

from opentelemetry.trace import Span, SpanKind
from pydantic import ValidationError
from redis.asyncio import Redis

from pipeline_doctor_agent.fix.models import FixOutcome
from pipeline_doctor_agent.investigator import InvestigationError, InvestigationResult
from pipeline_doctor_agent.observability import context_from_traceparent, metrics, span
from pipeline_doctor_agent.preprocess import ProcessedFailure
from pipeline_doctor_agent.schemas import PipelineFailedEvent
from pipeline_doctor_agent.ticket import TicketOutcome
from pipeline_doctor_agent.worker.consumer import Delivery, PermanentError

log = logging.getLogger("pipeline_doctor.handler")
DONE_TTL_S = 7 * 24 * 3600
LOCK_TTL_S = 120
LOCK_WAIT_S = 10.0
FIX_CLAIM_TTL_S = 3600


class Investigating(Protocol):
    async def investigate(self, event: PipelineFailedEvent) -> InvestigationResult: ...
    async def facts_for(self, event: PipelineFailedEvent) -> ProcessedFailure | None: ...


class Fixing(Protocol):
    async def attempt(self, result: InvestigationResult) -> FixOutcome: ...


class Ticketing(Protocol):
    async def record(
        self,
        result: InvestigationResult,
        *,
        pr_url: str | None = None,
        fix: FixOutcome | None = None,
    ) -> TicketOutcome: ...
    async def record_degraded(
        self, event: PipelineFailedEvent, reason: str, processed: ProcessedFailure | None
    ) -> TicketOutcome: ...
    def degraded_fingerprint(
        self, event: PipelineFailedEvent, processed: ProcessedFailure | None
    ) -> str: ...


class InProgress(RuntimeError):
    """Another worker holds a lease or lock; leave the message pending and retry later."""


class EventHandler:
    def __init__(
        self,
        redis: Redis,
        investigator: Investigating,
        tickets: Ticketing,
        *,
        fixer: Fixing | None = None,
        lease_s: int = 600,
    ) -> None:
        self.r, self.investigator, self.tickets, self.lease_s = (
            redis,
            investigator,
            tickets,
            lease_s,
        )
        self.fixer = fixer

    async def __call__(self, delivery: Delivery) -> None:
        try:
            event = PipelineFailedEvent.model_validate_json(delivery.body)
        except ValidationError as exc:
            raise PermanentError(
                f"invalid PipelineFailed event: {exc.error_count()} validation errors"
            ) from exc

        # Continue the trace the ingress started, so one trace id covers webhook to ticket.
        with span(
            "pipeline_doctor.event",
            parent=context_from_traceparent(event.traceparent),
            kind=SpanKind.CONSUMER,
            attributes={
                "pd.job": event.job_name,
                "pd.build": event.build_number,
                "pd.attempt": delivery.attempt,
            },
        ) as sp:
            await self._process(event, delivery, sp)

    async def _process(self, event: PipelineFailedEvent, delivery: Delivery, sp: Span) -> None:
        key = event.idempotency_key
        if await self.r.exists(f"pd:done:{key}"):
            log.info("duplicate delivery skipped", extra={"idempotency_key": key})
            metrics.current().event("duplicate")
            sp.set_attribute("pd.result", "duplicate")
            return
        if not await self.r.set(f"pd:lease:{key}", "1", nx=True, ex=self.lease_s):
            raise InProgress(f"{key} is being processed by another worker")
        try:
            cached = await self.r.get(f"pd:ticket:{key}")
            if (
                cached is not None
            ):  # an earlier delivery already wrote the ticket; only "done" is missing
                outcome = TicketOutcome.model_validate_json(cached)
                summary: dict[str, object] = {"resumed": True}
            else:
                outcome, summary = await self._investigate_and_ticket(event, delivery)
                await self.r.set(f"pd:ticket:{key}", outcome.model_dump_json(), ex=DONE_TTL_S)
                metrics.current().ticket(outcome.action, outcome.degraded)
            summary |= {
                "ticket": outcome.issue_key,
                "ticket_action": outcome.action,
                "degraded": outcome.degraded,
            }
            await self.r.set(f"pd:done:{key}", json.dumps(summary), ex=DONE_TTL_S)
            metrics.current().event("processed")
            sp.set_attributes(
                {
                    "pd.result": "processed",
                    "pd.ticket_action": outcome.action,
                    "pd.degraded": outcome.degraded,
                    "pd.fingerprint": str(summary.get("fingerprint", "")),
                }
            )
            log.info("event processed", extra={"idempotency_key": key, **summary})
        finally:
            await self.r.delete(f"pd:lease:{key}")

    async def _investigate_and_ticket(
        self, event: PipelineFailedEvent, delivery: Delivery
    ) -> tuple[TicketOutcome, dict[str, object]]:
        try:
            result = await self.investigator.investigate(event)
        except InvestigationError as exc:
            if not delivery.is_final:
                raise  # retried; the model may well work next time
            log.error(
                "diagnosis failed on the final attempt; writing a fallback ticket",
                extra={"kind": exc.kind},
            )
            processed = await self.investigator.facts_for(event)
            fingerprint = self.tickets.degraded_fingerprint(event, processed)
            async with self._lock(fingerprint):
                outcome = await self.tickets.record_degraded(event, f"{exc.kind}: {exc}", processed)
            return outcome, {"fingerprint": fingerprint, "degraded_reason": exc.kind}

        fix = await self._fix(event, result)
        with span("pipeline_doctor.ticket", attributes={"pd.fingerprint": result.fingerprint}):
            async with self._lock(result.fingerprint):
                outcome = await self.tickets.record(result, fix=fix)
        return outcome, {
            "fix": fix.status if fix else None,
            "pr_url": fix.pr_url if fix else None,
            "fingerprint": result.fingerprint,
            "category": result.diagnosis.category.value,
            "confidence": result.diagnosis.confidence,
            "action": result.decision.action.value,
            "steps": result.usage.steps,
        }

    async def _fix(
        self, event: PipelineFailedEvent, result: InvestigationResult
    ) -> FixOutcome | None:
        """Try an automatic fix at most once per event and once per failure, never twice in parallel.

        * ``fix:<key>``     this event already has an outcome (a redelivery reuses it, so no second PR)
        * ``fixpr:<fp>``    a pull request for this failure exists: link it instead of opening another
        * ``fixclaim:<fp>`` one run at a time per failure; the claim outlives a failed attempt (an
                            hour) so the same failure is not retried with the model over and over
        A fix is best effort: whatever happens here, the ticket is still written.
        """
        if self.fixer is None or not result.decision.allowed:
            return None
        key, fp = event.idempotency_key, result.fingerprint
        if (cached := await self.r.get(f"pd:fix:{key}")) is not None:
            return FixOutcome.model_validate_json(cached)
        if (existing := await self.r.get(f"pd:fixpr:{fp}")) is not None:
            outcome = FixOutcome(
                status="reused_pr",
                pr_url=str(existing),
                reason="a pull request for this failure already exists",
            )
        elif not await self._claim_fix(fp, key):
            outcome = FixOutcome(
                status="skipped",
                reason="a fix for this failure was already attempted or is in progress",
            )
        else:
            started = time.monotonic()
            with span("pipeline_doctor.fix", attributes={"pd.fingerprint": fp}) as sp:
                try:
                    outcome = await self.fixer.attempt(result)
                except Exception as exc:  # the fix must never block the ticket
                    log.exception("fix attempt crashed")
                    outcome = FixOutcome(
                        status="error", reason=f"unexpected error: {type(exc).__name__}"
                    )
                sp.set_attributes(
                    {"pd.fix.status": outcome.status, "pd.fix.attempts": outcome.attempts}
                )
            metrics.current().fix(outcome.status, time.monotonic() - started)
            if outcome.pr_url:
                await self.r.set(f"pd:fixpr:{fp}", outcome.pr_url, ex=DONE_TTL_S)
        await self.r.set(f"pd:fix:{key}", outcome.model_dump_json(), ex=DONE_TTL_S)
        return outcome

    async def _claim_fix(self, fingerprint: str, key: str) -> bool:
        name = f"pd:fixclaim:{fingerprint}"
        if await self.r.set(name, key, nx=True, ex=FIX_CLAIM_TTL_S):
            return True
        return await self.r.get(name) == key  # the same event resuming after a crash

    def _lock(self, fingerprint: str) -> _Lock:
        return _Lock(self.r, f"pd:ticketlock:{fingerprint}")


class _Lock:
    """A short Redis lock. It waits a few seconds instead of failing at once, so contention between
    two events for the same failure does not burn delivery attempts."""

    def __init__(self, redis: Redis, name: str) -> None:
        self.r, self.name, self.token = redis, name, uuid.uuid4().hex

    async def __aenter__(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOCK_WAIT_S
        while not await self.r.set(self.name, self.token, nx=True, ex=LOCK_TTL_S):
            if loop.time() >= deadline:
                raise InProgress(f"another worker is writing the ticket for {self.name}")
            await asyncio.sleep(0.1)

    async def __aexit__(self, *exc: object) -> None:
        # Not atomic (get then delete); the TTL bounds the damage if the lock expired and moved on.
        if await self.r.get(self.name) == self.token:
            await self.r.delete(self.name)
