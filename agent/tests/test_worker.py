from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import pytest
from mcp.client import Client
from pd_mcp_github.backend import MockBackend as GitHubMock
from pd_mcp_github.server import build_server as github_server
from pd_mcp_jira.backend import MockBackend as JiraMock
from pd_mcp_jira.server import build_server as jira_server
from pipeline_doctor_agent.fake_llm import ScriptedLlm, infra_scripts, logic_bug_scripts, reply
from pipeline_doctor_agent.investigator import InvestigationError, InvestigationResult, Investigator
from pipeline_doctor_agent.preprocess import ProcessedFailure, preprocess
from pipeline_doctor_agent.schemas import PipelineFailedEvent
from pipeline_doctor_agent.settings import Settings
from pipeline_doctor_agent.ticket import TicketOutcome, TicketService
from pipeline_doctor_agent.worker import handler as handler_module
from pipeline_doctor_agent.worker.consumer import Delivery, PermanentError, StreamConsumer
from pipeline_doctor_agent.worker.handler import EventHandler, InProgress

STREAM, GROUP = "pipeline-failed", "pipeline-doctor"
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
EVENT = {"idempotency_key": "demo/main:12", "job_name": "demo/main", "build_number": 12}


@pytest.fixture
async def redis() -> Any:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


def consumer(
    redis: Any, handler: Any, *, name: str = "w1", max_attempts: int = 3
) -> StreamConsumer:
    return StreamConsumer(
        redis,
        stream=STREAM,
        group=GROUP,
        consumer=name,
        handler=handler,
        max_attempts=max_attempts,
        min_idle_ms=20,
        block_ms=10,
    )


async def setup(redis: Any) -> None:
    await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)


async def pending(redis: Any) -> int:
    return int((await redis.xpending(STREAM, GROUP))["pending"])


async def drain(c: StreamConsumer, rounds: int = 8) -> None:
    for _ in range(rounds):
        await c.poll_once()
        await asyncio.sleep(0.03)  # exceed min_idle so failed messages are reclaimed


# ---- the consumer -----------------------------------------------------------------------------


async def test_success_acks(redis: Any) -> None:
    seen: list[str] = []

    async def ok(d: Delivery) -> None:
        seen.append(d.body)

    c = consumer(redis, ok)
    await setup(redis)
    await redis.xadd(STREAM, {"data": "hello"})
    await drain(c, 2)
    assert seen == ["hello"] and await pending(redis) == 0


async def test_retries_report_attempts_and_final_flag_then_dead_letter(redis: Any) -> None:
    seen: list[tuple[int, bool]] = []

    async def boom(d: Delivery) -> None:
        seen.append((d.attempt, d.is_final))
        raise RuntimeError("model unavailable")

    c = consumer(redis, boom, max_attempts=3)
    await setup(redis)
    await redis.xadd(STREAM, {"data": "payload"})
    await drain(c)
    assert seen == [(1, False), (2, False), (3, True)]
    dlq = await redis.xrange(f"{STREAM}-dlq")
    assert len(dlq) == 1
    assert dlq[0][1]["data"] == "payload" and dlq[0][1]["attempts"] == "3"
    assert "model unavailable" in dlq[0][1]["error"] and await pending(redis) == 0


async def test_permanent_error_goes_straight_to_dlq(redis: Any) -> None:
    async def bad(d: Delivery) -> None:
        raise PermanentError("malformed")

    c = consumer(redis, bad)
    await setup(redis)
    await redis.xadd(STREAM, {"data": "junk"})
    await drain(c, 2)
    dlq = await redis.xrange(f"{STREAM}-dlq")
    assert len(dlq) == 1 and dlq[0][1]["attempts"] == "1" and "permanent" in dlq[0][1]["error"]


async def test_message_of_a_crashed_worker_is_reclaimed(redis: Any) -> None:
    seen: list[Delivery] = []

    async def ok(d: Delivery) -> None:
        seen.append(d)

    c = consumer(redis, ok, name="alive")
    await setup(redis)
    await redis.xadd(STREAM, {"data": "orphan"})
    await redis.xreadgroup(GROUP, "dead", {STREAM: ">"}, count=1)  # read by a worker that then dies
    await drain(c, 3)
    assert [(d.body, d.attempt) for d in seen] == [("orphan", 2)] and await pending(redis) == 0


# ---- the handler, with fakes -----------------------------------------------------------------------


class FakeInvestigator:
    def __init__(
        self, result: InvestigationResult | None = None, error: InvestigationError | None = None
    ) -> None:
        self.calls, self.facts_calls, self.result, self.error = 0, 0, result, error
        self.processed: ProcessedFailure | None = preprocess(["ERROR: boom"])

    async def investigate(self, event: PipelineFailedEvent) -> InvestigationResult:
        self.calls += 1
        if self.error:
            raise self.error
        assert self.result is not None
        return replace(self.result, event=event)

    async def facts_for(self, event: PipelineFailedEvent) -> ProcessedFailure | None:
        self.facts_calls += 1
        return self.processed


class FakeTickets:
    def __init__(self, fail: Exception | None = None) -> None:
        self.recorded: list[str] = []
        self.fixes: list[Any] = []
        self.degraded: list[tuple[str, str]] = []
        self.fail = fail

    async def record(
        self, result: InvestigationResult, *, pr_url: str | None = None, fix: Any = None
    ) -> TicketOutcome:
        if self.fail:
            raise self.fail
        self.recorded.append(result.fingerprint)
        self.fixes.append(fix)
        return TicketOutcome(action="created", issue_key="DEMO-101", url="https://j/DEMO-101")

    async def record_degraded(
        self, event: PipelineFailedEvent, reason: str, processed: ProcessedFailure | None
    ) -> TicketOutcome:
        self.degraded.append((event.idempotency_key, reason))
        return TicketOutcome(action="created", issue_key="DEMO-102", degraded=True)

    def degraded_fingerprint(
        self, event: PipelineFailedEvent, processed: ProcessedFailure | None
    ) -> str:
        return TicketService.degraded_fingerprint(event, processed)


@pytest.fixture
def result(make_result: Any) -> InvestigationResult:
    return make_result()


def delivery(body: str | None = None, attempt: int = 1, max_attempts: int = 3) -> Delivery:
    return Delivery(body or json.dumps(EVENT), attempt, max_attempts)


async def test_duplicate_delivery_is_processed_and_ticketed_once(
    redis: Any, result: InvestigationResult
) -> None:
    inv, tickets = FakeInvestigator(result), FakeTickets()
    h = EventHandler(redis, inv, tickets)
    await h(delivery())
    await h(delivery())  # redelivery or ingress retry
    assert inv.calls == 1 and len(tickets.recorded) == 1
    summary = json.loads(await redis.get("pd:done:demo/main:12"))
    assert (
        summary["category"] == "code"
        and summary["ticket"] == "DEMO-101"
        and summary["degraded"] is False
    )


async def test_concurrent_delivery_backs_off(redis: Any, result: InvestigationResult) -> None:
    await redis.set("pd:lease:demo/main:12", "1", ex=60)  # another worker holds the lease
    inv = FakeInvestigator(result)
    with pytest.raises(InProgress):
        await EventHandler(redis, inv, FakeTickets())(delivery())
    assert inv.calls == 0


async def test_failure_before_the_final_attempt_is_retried_without_a_ticket(redis: Any) -> None:
    inv, tickets = FakeInvestigator(error=InvestigationError("timeout", "slow")), FakeTickets()
    h = EventHandler(redis, inv, tickets)
    with pytest.raises(InvestigationError):
        await h(delivery(attempt=2))
    assert tickets.degraded == [] and tickets.recorded == []
    assert not await redis.exists("pd:lease:demo/main:12") and not await redis.exists(
        "pd:done:demo/main:12"
    )


async def test_final_attempt_failure_writes_a_fallback_ticket_instead_of_vanishing(
    redis: Any,
) -> None:
    inv, tickets = (
        FakeInvestigator(error=InvestigationError("budget_exceeded", "too many calls")),
        FakeTickets(),
    )
    await EventHandler(redis, inv, tickets)(delivery(attempt=3))
    assert (
        tickets.degraded == [("demo/main:12", "budget_exceeded: budget_exceeded: too many calls")]
        and inv.facts_calls == 1
    )
    summary = json.loads(await redis.get("pd:done:demo/main:12"))
    assert (
        summary["degraded"] is True
        and summary["ticket"] == "DEMO-102"
        and summary["degraded_reason"] == "budget_exceeded"
    )


async def test_if_even_the_ticket_cannot_be_written_the_message_is_not_lost(
    redis: Any, result: InvestigationResult
) -> None:
    tickets = FakeTickets(fail=ConnectionError("jira down"))
    with pytest.raises(ConnectionError):
        await EventHandler(redis, FakeInvestigator(result), tickets)(delivery(attempt=3))
    assert not await redis.exists(
        "pd:done:demo/main:12"
    )  # stays pending -> DLQ, where the alert fires


async def test_a_redelivery_after_the_ticket_was_written_skips_straight_to_done(
    redis: Any, result: InvestigationResult
) -> None:
    await redis.set(
        "pd:ticket:demo/main:12",
        TicketOutcome(action="created", issue_key="DEMO-55").model_dump_json(),
    )
    inv, tickets = FakeInvestigator(result), FakeTickets()
    await EventHandler(redis, inv, tickets)(delivery())
    assert inv.calls == 0 and tickets.recorded == []  # no second investigation, no second ticket
    assert json.loads(await redis.get("pd:done:demo/main:12"))["ticket"] == "DEMO-55"


async def test_ticket_writes_for_one_fingerprint_are_serialised(
    redis: Any, result: InvestigationResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler_module, "LOCK_WAIT_S", 0.3)
    lock = f"pd:ticketlock:{result.fingerprint}"
    await redis.set(lock, "someone-else", ex=60)
    tickets = FakeTickets()
    with pytest.raises(InProgress):  # waits, gives up, leaves the message for a retry
        await EventHandler(redis, FakeInvestigator(result), tickets)(delivery())
    assert tickets.recorded == []

    async def release_soon() -> None:
        await asyncio.sleep(0.15)
        await redis.delete(lock)

    monkeypatch.setattr(handler_module, "LOCK_WAIT_S", 3.0)
    waiter = asyncio.create_task(release_soon())
    await redis.delete("pd:lease:demo/main:12")
    await redis.set(lock, "someone-else", ex=60)
    await EventHandler(redis, FakeInvestigator(result), tickets)(delivery())  # waits, then proceeds
    await waiter
    assert tickets.recorded == [result.fingerprint] and not await redis.exists(lock)


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "{}",
        json.dumps({**EVENT, "job_name": "x;rm"}),
        json.dumps({**EVENT, "build_number": 0}),
        json.dumps({**EVENT, "build_url": "javascript:alert(1)"}),
    ],
)
async def test_malformed_events_are_permanent_errors(redis: Any, body: str) -> None:
    with pytest.raises(PermanentError):
        await EventHandler(redis, FakeInvestigator(), FakeTickets())(delivery(body))


# ---- queue -> investigation -> ticket, with real MCP servers and a scripted model --------------------------


class World:
    """Real Investigator (real MCP servers over stdio, scripted LLM) + TicketService on the mock Jira/GitHub."""

    def __init__(
        self,
        settings: Settings,
        redis: Any,
        scripts: dict[str, list[dict[str, Any]]],
        **overrides: Any,
    ) -> None:
        self.jira, self.github = JiraMock.load(FIXTURES), GitHubMock.load(FIXTURES)
        self.llm = ScriptedLlm(scripts=scripts)
        settings = replace(settings, **overrides)
        self.investigator = Investigator(settings, small_model=self.llm, large_model=self.llm)
        self.tickets = TicketService(
            settings,
            jira=lambda: Client(jira_server(self.jira)),
            github=lambda: Client(github_server(self.github)),
        )
        self.consumer = consumer(redis, EventHandler(redis, self.investigator, self.tickets))


async def test_logic_bug_goes_from_queue_to_a_new_assigned_ticket(
    redis: Any, settings: Settings
) -> None:
    world = World(settings, redis, logic_bug_scripts())
    event = {
        "idempotency_key": "demo-service/main:12",
        "job_name": "demo-service/main",
        "build_number": 12,
        "repo": "acme/demo-service",
        "commit_sha": "c3d4e5f60718293a4b5c6d7e8f90123456789012",
        "build_url": "https://jenkins.example/job/demo-service/main/12/",
        "received_at": "2026-11-12T12:00:00Z",
        "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
    }
    await setup(redis)
    await redis.xadd(STREAM, {"data": json.dumps(event)})
    await redis.xadd(STREAM, {"data": json.dumps(event)})  # duplicate delivery
    await drain(world.consumer, 3)

    summary = json.loads(await redis.get("pd:done:demo-service/main:12"))
    assert (
        summary["category"] == "code"
        and summary["action"] == "fix"
        and summary["ticket_action"] == "created"
    )
    created = [i for i in world.jira.issues.values() if i.category == "code"]
    assert (
        len(created) == 1 and created[0].assignee == "dev1"
    )  # CODEOWNERS: /app/pricing.py -> @dev1
    assert "apply_discount returns price * rate" in created[0].description
    assert await pending(redis) == 0 and await redis.xlen(f"{STREAM}-dlq") == 0
    assert world.llm.scripts["diagnose_agent"] == []  # the investigation ran exactly once


async def test_infra_failure_is_recognised_as_the_seeded_ticket_and_only_commented(
    redis: Any, settings: Settings
) -> None:
    """The fingerprint computed live through the whole pipeline must equal the one seeded on DEMO-7."""
    world = World(settings, redis, infra_scripts())
    event = {
        "idempotency_key": "payments-api/main:30",
        "job_name": "payments-api/main",
        "build_number": 30,
        "repo": "acme/payments-api",
        "commit_sha": "d4e5f60718293a4b5c6d7e8f9012345678901234",
    }
    await setup(redis)
    await redis.xadd(STREAM, {"data": json.dumps(event)})
    await drain(world.consumer, 2)

    summary = json.loads(await redis.get("pd:done:payments-api/main:30"))
    assert summary["fingerprint"] == "fdacb5a1cf4e5ebe"
    assert (summary["category"], summary["action"]) == ("infrastructure", "ticket_only")
    assert (summary["ticket"], summary["ticket_action"]) == ("DEMO-7", "commented")
    assert len(world.jira.issues) == 2  # no duplicate ticket
    comment = world.jira.issues["DEMO-7"].comments[0]
    assert "No automatic fix" in comment and "never auto-fixed" in comment
    assert (
        world.github.branches == {} and world.github.pull_requests == []
    )  # infra failures never touch code


async def test_model_outage_ends_with_a_fallback_ticket_after_the_retries(
    redis: Any, settings: Settings
) -> None:
    # A token budget of 1 makes every attempt fail: the model "is unavailable".
    # Every attempt trips the budget on its first model call; give the fake enough steps for 3 attempts.
    steps = {
        role: [reply("x")] * 5 for role in ("log_analyst", "code_investigator", "diagnose_agent")
    }
    world = World(settings, redis, steps, token_budget=1)
    event = {
        "idempotency_key": "demo-service/main:12",
        "job_name": "demo-service/main",
        "build_number": 12,
        "repo": "acme/demo-service",
    }
    await setup(redis)
    await redis.xadd(STREAM, {"data": json.dumps(event)})
    await drain(world.consumer, 10)

    assert await redis.xrange(f"{STREAM}-dlq") == []  # handled by the fallback, not dead-lettered
    summary = json.loads(await redis.get("pd:done:demo-service/main:12"))
    assert summary["degraded"] is True and summary["degraded_reason"] == "budget_exceeded"
    fallback = [i for i in world.jira.issues.values() if i.category == "unknown"]
    assert len(fallback) == 1 and "Automatic diagnosis was unavailable" in fallback[0].description
    assert (
        "assert 10.0 == 90.0" in fallback[0].description
    )  # the scrubbed excerpt, fetched without any LLM
    assert (
        await redis.xlen(f"{STREAM}-dlq") == 0 and await pending(redis) == 0
    )  # handled, not dead-lettered


# ---- automatic fixes in the handler --------------------------------------------------------------------


from pipeline_doctor_agent.fix.models import FixOutcome  # noqa: E402

PR = "https://github.example/acme/demo-service/pull/7"


class FakeFixer:
    def __init__(self, outcome: FixOutcome | None = None, error: Exception | None = None) -> None:
        self.calls = 0
        self.outcome = outcome or FixOutcome(
            status="pr_opened", pr_url=PR, attempts=1, verification="2 of 3 failed before; all pass"
        )
        self.error = error

    async def attempt(self, result: InvestigationResult) -> FixOutcome:
        self.calls += 1
        if self.error:
            raise self.error
        return self.outcome


def fix_handler(
    redis: Any, result: InvestigationResult, fixer: FakeFixer | None
) -> tuple[EventHandler, FakeTickets]:
    tickets = FakeTickets()
    return EventHandler(redis, FakeInvestigator(result), tickets, fixer=fixer), tickets


async def test_an_allowed_fix_runs_once_and_its_outcome_reaches_the_ticket(
    redis: Any, result: InvestigationResult
) -> None:
    fixer = FakeFixer()
    h, tickets = fix_handler(redis, result, fixer)
    await h(delivery())
    await h(delivery())  # redelivery
    assert fixer.calls == 1 and [f.pr_url for f in tickets.fixes] == [PR]
    summary = json.loads(await redis.get("pd:done:demo/main:12"))
    assert summary["fix"] == "pr_opened" and summary["pr_url"] == PR
    assert await redis.get(f"pd:fixpr:{result.fingerprint}") == PR


async def test_no_fix_when_the_gate_says_no_or_no_fixer_is_configured(
    redis: Any, make_result: Any, result: InvestigationResult
) -> None:
    low = make_result(confidence=0.3)
    fixer = FakeFixer()
    h, tickets = fix_handler(redis, low, fixer)
    await h(delivery())
    assert fixer.calls == 0 and tickets.fixes == [None]
    h2, tickets2 = fix_handler(redis, result, None)
    await h2(delivery(json.dumps({**EVENT, "idempotency_key": "demo/main:13"})))
    assert tickets2.fixes == [None]


async def test_a_redelivery_after_the_fix_but_before_the_ticket_does_not_fix_again(
    redis: Any, result: InvestigationResult
) -> None:
    await redis.set(
        "pd:fix:demo/main:12", FixOutcome(status="pr_opened", pr_url=PR).model_dump_json()
    )
    fixer = FakeFixer()
    h, tickets = fix_handler(redis, result, fixer)
    await h(delivery())
    assert fixer.calls == 0 and tickets.fixes[0].pr_url == PR  # the ticket still gets the PR link


async def test_an_existing_pull_request_for_the_same_failure_is_linked_not_duplicated(
    redis: Any, result: InvestigationResult
) -> None:
    await redis.set(f"pd:fixpr:{result.fingerprint}", PR)
    fixer = FakeFixer()
    h, tickets = fix_handler(redis, result, fixer)
    await h(delivery())
    assert (
        fixer.calls == 0
        and tickets.fixes[0].status == "reused_pr"
        and tickets.fixes[0].pr_url == PR
    )


async def test_one_fix_attempt_per_failure_even_when_a_second_build_fails_the_same_way(
    redis: Any, result: InvestigationResult
) -> None:
    fixer = FakeFixer(FixOutcome(status="failed", reason="no passing fix after 3 attempts"))
    h, tickets = fix_handler(redis, result, fixer)
    await h(delivery())
    other = json.dumps({**EVENT, "idempotency_key": "demo/main:13", "build_number": 13})
    await h(delivery(other))
    assert fixer.calls == 1  # the claim outlives the failed attempt: no model spend on a repeat
    assert tickets.fixes[1].status == "skipped" and "already attempted" in tickets.fixes[1].reason


async def test_the_same_event_resuming_after_a_crash_may_continue_its_own_claim(
    redis: Any, result: InvestigationResult
) -> None:
    await redis.set(
        f"pd:fixclaim:{result.fingerprint}", "demo/main:12"
    )  # claimed by this very event, then it crashed
    fixer = FakeFixer()
    h, _ = fix_handler(redis, result, fixer)
    await h(delivery())
    assert fixer.calls == 1


async def test_a_crashing_fixer_never_blocks_the_ticket(
    redis: Any, result: InvestigationResult
) -> None:
    h, tickets = fix_handler(redis, result, FakeFixer(error=RuntimeError("docker exploded")))
    await h(delivery())
    assert tickets.recorded == [result.fingerprint]
    assert tickets.fixes[0].status == "error" and "RuntimeError" in tickets.fixes[0].reason
    assert "docker exploded" not in tickets.fixes[0].reason  # internals are not copied into tickets
