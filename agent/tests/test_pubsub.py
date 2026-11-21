"""The Pub/Sub consumer: behaviour with fakes, and against the real Pub/Sub emulator in Docker."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from pipeline_doctor_agent.observability import Metrics, install
from pipeline_doctor_agent.observability.metrics import current
from pipeline_doctor_agent.settings import Settings
from pipeline_doctor_agent.worker.consumer import Delivery, PermanentError, StreamConsumer
from pipeline_doctor_agent.worker.factory import build_consumer
from pipeline_doctor_agent.worker.pubsub_consumer import PubSubConsumer

PROJECT, SUB, DLQ = "demo-project", "worker", "pipeline-failed-dlq"


@pytest.fixture(autouse=True)
def fresh_metrics() -> Iterator[Metrics]:
    previous = current()
    yield install(Metrics())
    install(previous)


def value(m: Metrics, name: str, **labels: str) -> float:
    return m.registry.get_sample_value(name, labels) or 0.0


# ---- with fakes: the consumer's own logic ---------------------------------------------------------------


class FakeSubscriber:
    def __init__(self, items: list[Any]) -> None:
        self.items, self.acked, self.deadlines = items, [], []

    def subscription_path(self, project: str, name: str) -> str:
        return f"projects/{project}/subscriptions/{name}"

    def pull(self, request: dict[str, Any], timeout: float) -> Any:
        taken, self.items = self.items[:1], self.items[1:]
        return SimpleNamespace(received_messages=taken)

    def acknowledge(self, request: dict[str, Any]) -> None:
        self.acked += request["ack_ids"]

    def modify_ack_deadline(self, request: dict[str, Any]) -> None:
        self.deadlines.append((tuple(request["ack_ids"]), request["ack_deadline_seconds"]))


class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, bytes, dict[str, str]]] = []
        self.fail = fail

    def topic_path(self, project: str, name: str) -> str:
        return f"projects/{project}/topics/{name}"

    def publish(self, topic: str, data: bytes, **attrs: str) -> Any:
        if self.fail:
            raise RuntimeError("publish failed")
        self.sent.append((topic, data, attrs))
        return SimpleNamespace(result=lambda _t=None: "id")


def item(body: str = "{}", attempt: int = 1, ack_id: str = "a1") -> Any:
    return SimpleNamespace(
        ack_id=ack_id,
        message=SimpleNamespace(data=body.encode(), message_id="m-" + ack_id),
        delivery_attempt=attempt,
    )


def consumer(sub: FakeSubscriber, pub: FakePublisher, handler: Any, **kw: Any) -> PubSubConsumer:
    return PubSubConsumer(
        project=PROJECT,
        subscription=SUB,
        dead_letter_topic=DLQ,
        handler=handler,
        subscriber=sub,
        publisher=pub,
        **kw,
    )


async def test_success_acknowledges_and_reports_the_delivery_attempt() -> None:
    seen: list[Delivery] = []

    async def ok(d: Delivery) -> None:
        seen.append(d)

    sub = FakeSubscriber([item("hello", attempt=3)])
    assert await consumer(sub, FakePublisher(), ok).poll_once() == 1
    assert seen == [Delivery("hello", 3, 5)] and sub.acked == ["a1"]
    assert sub.deadlines == []  # no failure, no nack


async def test_a_missing_attempt_counter_counts_as_the_first_delivery() -> None:
    seen: list[int] = []

    async def ok(d: Delivery) -> None:
        seen.append(d.attempt)

    await consumer(FakeSubscriber([item(attempt=0)]), FakePublisher(), ok).poll_once()
    assert seen == [1]


async def test_a_failing_handler_is_not_acknowledged_and_is_nacked(fresh_metrics: Metrics) -> None:
    async def boom(_d: Delivery) -> None:
        raise RuntimeError("model unavailable")

    sub = FakeSubscriber([item(attempt=2)])
    await consumer(sub, FakePublisher(), boom).poll_once()
    assert sub.acked == [] and sub.deadlines == [(("a1",), 0)]  # deadline 0 = redeliver now
    assert value(fresh_metrics, "pd_events_total", result="failed") == 1
    assert value(fresh_metrics, "pd_events_total", result="dead_lettered") == 0


async def test_the_final_failed_attempt_is_counted_as_dead_lettered(fresh_metrics: Metrics) -> None:
    final: list[bool] = []

    async def boom(d: Delivery) -> None:
        final.append(d.is_final)
        raise RuntimeError("x")

    sub = FakeSubscriber([item(attempt=5)])
    await consumer(sub, FakePublisher(), boom, max_attempts=5).poll_once()
    assert (
        final == [True] and sub.acked == []
    )  # the subscription's dead-letter policy takes it from here
    assert value(fresh_metrics, "pd_events_total", result="dead_lettered") == 1


async def test_a_permanent_error_goes_to_the_dead_letter_topic_and_is_acknowledged(
    fresh_metrics: Metrics,
) -> None:
    async def bad(_d: Delivery) -> None:
        raise PermanentError("invalid PipelineFailed event")

    sub, pub = FakeSubscriber([item('{"x":1}', attempt=1)]), FakePublisher()
    await consumer(sub, pub, bad).poll_once()
    (topic, data, attrs) = pub.sent[0]
    assert topic.endswith(f"/topics/{DLQ}") and data == b'{"x":1}'
    assert (
        attrs["error"].startswith("permanent: invalid")
        and attrs["attempts"] == "1"
        and attrs["source_id"] == "m-a1"
    )
    assert sub.acked == ["a1"]
    assert value(fresh_metrics, "pd_events_total", result="dead_lettered") == 1


async def test_if_the_dead_letter_publish_fails_the_message_is_kept_not_lost() -> None:
    async def bad(_d: Delivery) -> None:
        raise PermanentError("bad")

    sub = FakeSubscriber([item()])
    await consumer(sub, FakePublisher(fail=True), bad).poll_once()
    assert sub.acked == [] and sub.deadlines == [(("a1",), 0)]


async def test_a_long_handler_keeps_its_lease() -> None:
    async def slow(_d: Delivery) -> None:
        await asyncio.sleep(2.4)

    sub = FakeSubscriber([item()])
    c = consumer(
        sub, FakePublisher(), slow, ack_deadline_s=3
    )  # extends every max(1, 3/3) = 1 second
    await c.poll_once()
    assert len(sub.deadlines) >= 2 and all(d == (("a1",), 3) for d in sub.deadlines)
    assert sub.acked == ["a1"]
    n = len(sub.deadlines)
    await asyncio.sleep(1.3)
    assert len(sub.deadlines) == n  # extension stops when the handler is done


async def test_an_empty_queue_is_not_an_error() -> None:
    async def never(_d: Delivery) -> None:
        raise AssertionError

    assert await consumer(FakeSubscriber([]), FakePublisher(), never).poll_once() == 0


@pytest.mark.parametrize("attempts", [0, 1, 3, 4, 101])
def test_max_attempts_must_fit_pubsubs_dead_letter_range(attempts: int) -> None:
    async def h(_d: Delivery) -> None: ...

    with pytest.raises(ValueError, match="max_attempts"):
        consumer(FakeSubscriber([]), FakePublisher(), h, max_attempts=attempts)


def test_settings_choose_and_validate_the_backend() -> None:
    assert Settings().queue_backend == "redis"
    s = Settings.from_env({"QUEUE_BACKEND": "pubsub", "PUBSUB_PROJECT": "p", "MAX_ATTEMPTS": "5"})
    assert (s.queue_backend, s.pubsub_project, s.pubsub_subscription) == (
        "pubsub",
        "p",
        "pipeline-doctor-worker",
    )
    with pytest.raises(ValueError, match="PUBSUB_PROJECT"):
        Settings.from_env({"QUEUE_BACKEND": "pubsub"})
    with pytest.raises(ValueError, match="QUEUE_BACKEND"):
        Settings.from_env({"QUEUE_BACKEND": "kafka"})
    with pytest.raises(ValueError, match="ACK_DEADLINE"):
        Settings.from_env({"PUBSUB_ACK_DEADLINE_SECONDS": "5"})


async def test_the_factory_builds_the_right_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def h(_d: Delivery) -> None: ...

    monkeypatch.setenv("PUBSUB_EMULATOR_HOST", "localhost:1")  # no credentials needed to construct
    redis_c = build_consumer(Settings(), None, h, lease_s=100)  # type: ignore[arg-type]
    assert isinstance(redis_c, StreamConsumer) and redis_c.min_idle_ms == 100_000
    ps = build_consumer(
        Settings(queue_backend="pubsub", pubsub_project="p", max_attempts=5), None, h, lease_s=100
    )  # type: ignore[arg-type]
    assert (
        isinstance(ps, PubSubConsumer)
        and ps.subscription == "projects/p/subscriptions/pipeline-doctor-worker"
    )


# ---- against the real emulator ----------------------------------------------------------------------------

EMULATOR_IMAGE = "gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators"


def _docker_ready() -> bool:
    if os.environ.get("PD_SKIP_DOCKER_TESTS") or shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", EMULATOR_IMAGE], capture_output=True)
    return r.returncode == 0  # never pulls on its own: ~1.5 GB


needs_emulator = pytest.mark.skipif(
    not _docker_ready(), reason="Pub/Sub emulator image not available"
)


@pytest.fixture(scope="module")
def emulator() -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cid = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-p",
            f"127.0.0.1:{port}:8085",
            EMULATOR_IMAGE,
            "gcloud",
            "beta",
            "emulators",
            "pubsub",
            "start",
            "--host-port=0.0.0.0:8085",
            f"--project={PROJECT}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("emulator did not start")
        os.environ["PUBSUB_EMULATOR_HOST"] = f"127.0.0.1:{port}"
        yield f"127.0.0.1:{port}"
    finally:
        os.environ.pop("PUBSUB_EMULATOR_HOST", None)
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


class Real:
    """A topic, subscription and dead-letter topic with a pull subscription, in the emulator."""

    def __init__(self, tag: str, ack_deadline: int = 10) -> None:
        from google.cloud import pubsub_v1  # type: ignore[import-untyped]

        self.pub, self.sub = pubsub_v1.PublisherClient(), pubsub_v1.SubscriberClient()
        self.topic = self.pub.topic_path(PROJECT, f"events-{tag}")
        self.dlq = self.pub.topic_path(PROJECT, f"dlq-{tag}")
        self.sub_path = self.sub.subscription_path(PROJECT, f"worker-{tag}")
        self.dlq_sub = self.sub.subscription_path(PROJECT, f"dlq-reader-{tag}")
        self.tag = tag
        self.pub.create_topic(request={"name": self.topic})
        self.pub.create_topic(request={"name": self.dlq})
        self.sub.create_subscription(
            request={
                "name": self.sub_path,
                "topic": self.topic,
                "ack_deadline_seconds": ack_deadline,
            }
        )
        self.sub.create_subscription(request={"name": self.dlq_sub, "topic": self.dlq})

    def consumer(self, handler: Any, **kw: Any) -> PubSubConsumer:
        return PubSubConsumer(
            project=PROJECT,
            subscription=f"worker-{self.tag}",
            dead_letter_topic=f"dlq-{self.tag}",
            handler=handler,
            subscriber=self.sub,
            publisher=self.pub,
            pull_timeout_s=5.0,
            **kw,
        )

    def publish(self, body: str) -> None:
        self.pub.publish(self.topic, body.encode()).result(10)

    def dlq_messages(self) -> list[Any]:
        return list(
            self.sub.pull(
                request={"subscription": self.dlq_sub, "max_messages": 10}, timeout=5
            ).received_messages
        )


@needs_emulator
async def test_emulator_a_message_is_delivered_once_and_acknowledged(emulator: str) -> None:
    r, got = Real("happy"), []

    async def ok(d: Delivery) -> None:
        got.append(d.body)

    c = r.consumer(ok)
    r.publish('{"idempotency_key":"demo/main:1"}')
    assert await c.poll_once() == 1 and got == ['{"idempotency_key":"demo/main:1"}']
    assert await c.poll_once() == 0  # acknowledged: not delivered again


@needs_emulator
async def test_emulator_a_failure_is_redelivered_then_succeeds(emulator: str) -> None:
    r, calls = Real("retry"), []

    async def flaky(d: Delivery) -> None:
        calls.append(d.body)
        if len(calls) == 1:
            raise RuntimeError("first attempt fails")

    c = r.consumer(flaky)
    r.publish("payload")
    await c.poll_once()
    for _ in range(5):  # nack makes it immediately available again
        if await c.poll_once():
            break
    assert calls == ["payload", "payload"]
    assert await c.poll_once() == 0


@needs_emulator
async def test_emulator_a_permanent_error_lands_in_the_dead_letter_topic(emulator: str) -> None:
    r = Real("perm")

    async def bad(_d: Delivery) -> None:
        raise PermanentError("invalid PipelineFailed event: 2 validation errors")

    c = r.consumer(bad)
    r.publish("not json")
    assert await c.poll_once() == 1
    assert await c.poll_once() == 0  # acknowledged on the main subscription
    (m,) = r.dlq_messages()
    assert m.message.data == b"not json"
    attrs = dict(m.message.attributes)
    assert (
        attrs["error"].startswith("permanent: invalid")
        and attrs["attempts"] == "1"
        and "source_id" in attrs
    )


@needs_emulator
async def test_emulator_a_handler_longer_than_the_ack_deadline_is_not_redelivered(
    emulator: str,
) -> None:
    """10 s is Pub/Sub's minimum ack deadline; the handler takes 14 s. The lease extension must hold it."""
    r, calls = Real("lease", ack_deadline=10), []

    async def slow(d: Delivery) -> None:
        calls.append(time.time())
        await asyncio.sleep(14)

    c = r.consumer(slow, ack_deadline_s=10)
    r.publish("slow work")
    other = r.consumer(slow, ack_deadline_s=10)  # a second worker competing for the message

    async def competitor() -> int:
        await asyncio.sleep(11)  # after the original deadline would have expired
        return await other.poll_once()

    handled, stolen = await asyncio.gather(c.poll_once(), competitor())
    assert handled == 1 and stolen == 0 and len(calls) == 1
