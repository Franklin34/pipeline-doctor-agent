"""Timeout, cleanup, caching and error paths, with a fake `docker` (no Docker needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pd_sandbox import DockerSandbox, Limits, SandboxError
from pd_sandbox.docker import Proc, run_process


class FakeDocker:
    def __init__(self, *, test_proc: Proc | None = None, install_proc: Proc | None = None) -> None:
        self.calls: list[list[str]] = []
        self.test_proc = test_proc or Proc(0, b"1 passed")
        self.install_proc = install_proc or Proc(0, b"installed")

    async def __call__(self, args: list[str], timeout_s: float, max_bytes: int) -> Proc:
        self.calls.append(args)
        if args[1:3] == ["rm", "-f"] or args[1] == "ps":
            return Proc(0, b"")
        return self.install_proc if "pip" in args else self.test_proc

    def verbs(self) -> list[str]:
        return [" ".join(a[1:3]) for a in self.calls]


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n")
    return d


async def test_timeout_removes_the_container_not_just_the_cli(tmp_path: Path, ws: Path) -> None:
    fake = FakeDocker(test_proc=Proc(None, b"partial", timed_out=True))
    sb = DockerSandbox(exec_=fake, cache_dir=tmp_path / "c")
    res = await sb.run_tests(ws, await sb.prepare(None))
    assert res.timed_out and res.exit_code is None and not res.passed
    assert (
        "rm -f" in fake.verbs()
    )  # `docker run` was killed, so the container itself must be removed
    removed = next(a for a in fake.calls if a[1:3] == ["rm", "-f"])
    started = next(a for a in fake.calls if a[1] == "run")
    assert removed[-1] == started[started.index("--name") + 1]


async def test_cancellation_removes_the_container(tmp_path: Path, ws: Path) -> None:
    calls: list[list[str]] = []

    async def slow(args: list[str], timeout_s: float, max_bytes: int) -> Proc:
        calls.append(args)
        if args[1] == "run":
            await asyncio.sleep(30)
        return Proc(0, b"")

    sb = DockerSandbox(exec_=slow, cache_dir=tmp_path / "c")
    deps = await sb.prepare(None)
    task = asyncio.create_task(sb.run_tests(ws, deps))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(a[1:3] == ["rm", "-f"] for a in calls)


async def test_results_directory_is_always_deleted(
    tmp_path: Path, ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    made: list[Path] = []
    real = tempfile.mkdtemp

    def spy(*a: object, **k: object) -> str:
        p = real(*a, **k)  # type: ignore[arg-type]
        made.append(Path(p))
        return p

    monkeypatch.setattr("pd_sandbox.docker.tempfile.mkdtemp", spy)
    sb = DockerSandbox(exec_=FakeDocker(), cache_dir=tmp_path / "c")
    await sb.run_tests(ws, await sb.prepare(None))
    assert made and not any(p.exists() for p in made)


async def test_dependencies_are_installed_once_per_requirements_hash(tmp_path: Path) -> None:
    fake = FakeDocker()
    sb = DockerSandbox(exec_=fake, cache_dir=tmp_path / "c")
    a = await sb.prepare("pytest==9.2.0\n")
    b = await sb.prepare("pytest==9.2.0\n")
    c = await sb.prepare("pytest==9.2.0\nrich==14.0.0\n")
    installs = [x for x in fake.calls if "pip" in x]
    assert a == b != c and len(installs) == 2


async def test_failed_install_is_a_sandbox_error_and_is_not_cached(tmp_path: Path) -> None:
    fake = FakeDocker(install_proc=Proc(1, b"ERROR: No matching distribution found for evil-pkg"))
    sb = DockerSandbox(exec_=fake, cache_dir=tmp_path / "c")
    for _ in range(2):  # a failure must not poison the cache
        with pytest.raises(SandboxError, match="No matching distribution"):
            await sb.prepare("evil-pkg==1.0\n")
    assert len([x for x in fake.calls if "pip" in x]) == 2


async def test_install_timeout(tmp_path: Path) -> None:
    sb = DockerSandbox(
        exec_=FakeDocker(install_proc=Proc(None, b"", timed_out=True)), cache_dir=tmp_path / "c"
    )
    with pytest.raises(SandboxError, match="timed out"):
        await sb.prepare("slow==1\n")


async def test_docker_missing_or_daemon_down_are_sandbox_errors_not_test_failures(
    tmp_path: Path, ws: Path
) -> None:
    async def missing(args: list[str], t: float, m: int) -> Proc:
        raise FileNotFoundError("docker")

    with pytest.raises(SandboxError, match="not installed"):
        await DockerSandbox(exec_=missing, cache_dir=tmp_path / "a").run_tests(
            ws, await DockerSandbox(exec_=FakeDocker(), cache_dir=tmp_path / "a").prepare(None)
        )

    sb = DockerSandbox(
        exec_=FakeDocker(test_proc=Proc(125, b"Cannot connect to the Docker daemon")),
        cache_dir=tmp_path / "b",
    )
    with pytest.raises(SandboxError, match="could not start"):
        await sb.run_tests(ws, await sb.prepare(None))


async def test_output_is_capped_and_only_the_tail_is_kept(tmp_path: Path, ws: Path) -> None:
    sb = DockerSandbox(
        exec_=FakeDocker(test_proc=Proc(1, b"A" * 50_000 + b"THE-END")), cache_dir=tmp_path / "c"
    )
    res = await sb.run_tests(ws, await sb.prepare(None))
    assert len(res.output_excerpt) < 4_100 and res.output_excerpt.endswith("THE-END")


async def test_orphans_are_swept(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    async def fake(args: list[str], t: float, m: int) -> Proc:
        calls.append(args)
        return Proc(0, b"abc123\ndef456\n" if args[1] == "ps" else b"")

    assert await DockerSandbox(exec_=fake, cache_dir=tmp_path).cleanup_orphans() == 2
    assert calls[0][-1] == "label=pd-sandbox=1" and calls[1][:3] == ["docker", "rm", "-f"]


async def test_run_process_really_times_out_and_caps_output() -> None:
    slow = await run_process(["python3", "-c", "import time; time.sleep(30)"], 0.5, 1000)
    assert slow.timed_out and slow.returncode is None
    big = await run_process(["python3", "-c", "print('x' * 500000)"], 10, 1000)
    assert (
        not big.timed_out and big.returncode == 0 and len(big.output) == 1000
    )  # child not blocked by the cap


def test_limits_defaults_are_conservative() -> None:
    lim = Limits()
    assert (lim.memory, lim.cpus, lim.pids, lim.test_timeout_s) == ("512m", 1.0, 128, 120.0)
