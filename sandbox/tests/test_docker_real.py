"""Isolation, proven against real Docker. Skipped when Docker or the image is not available.

Each test plays the hostile patch: it tries something and the assertion is that it cannot.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pd_sandbox import DEFAULT_IMAGE, DockerSandbox, Limits, SandboxError, judge

IMAGE = os.environ.get("PD_SANDBOX_IMAGE", DEFAULT_IMAGE)


def _docker_ready() -> bool:
    if os.environ.get("PD_SKIP_DOCKER_TESTS") or shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=20
            ).returncode
            == 0
        )
    except (subprocess.TimeoutExpired, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _docker_ready(), reason=f"Docker or the image {IMAGE} is not available"
)


@pytest.fixture(scope="session")
def cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("deps-cache")


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d


def sandbox(cache: Path, **limits: object) -> DockerSandbox:
    return DockerSandbox(IMAGE, Limits(**limits), cache_dir=cache)  # type: ignore[arg-type]


async def probe(cache: Path, ws: Path, code: str, **limits: object):  # type: ignore[no-untyped-def]
    sb = sandbox(cache, **limits)
    return await sb.run_tests(ws, await sb.prepare(None), command=("python", "-c", code))


def containers() -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=pd-sandbox=1"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


async def test_runs_as_nobody_with_no_capabilities_and_no_new_privileges(
    cache: Path, ws: Path
) -> None:
    r = await probe(
        cache,
        ws,
        "import os; print('uid', os.getuid(), os.getgid()); print(open('/proc/self/status').read())",
    )
    assert "uid 65534 65534" in r.output_excerpt
    assert "CapEff:\t0000000000000000" in r.output_excerpt and "NoNewPrivs:\t1" in r.output_excerpt


async def test_there_is_no_network_at_all(cache: Path, ws: Path) -> None:
    code = (
        "import socket\n"
        "for fn in (lambda: socket.create_connection(('1.1.1.1', 53), 3), lambda: socket.gethostbyname('pypi.org')):\n"
        "    try:\n        fn(); print('NETWORK-REACHED')\n    except OSError as e:\n        print('blocked:', type(e).__name__)\n"
    )
    r = await probe(cache, ws, code)
    assert "NETWORK-REACHED" not in r.output_excerpt and r.output_excerpt.count("blocked:") == 2


async def test_root_filesystem_and_the_repository_are_read_only(cache: Path, ws: Path) -> None:
    code = (
        "for p in ('/etc/pd-probe', '/usr/pd-probe', '/work/pd-probe', '/deps/pd-probe'):\n"
        "    try:\n        open(p, 'w').write('x'); print('WROTE', p)\n    except OSError as e:\n        print('denied', p)\n"
        "open('/tmp/ok', 'w').write('x'); open('/results/ok', 'w').write('x'); print('tmp+results writable')\n"
    )
    r = await probe(cache, ws, code)
    assert "WROTE" not in r.output_excerpt and r.output_excerpt.count("denied") == 4
    assert "tmp+results writable" in r.output_excerpt
    assert not (ws / "pd-probe").exists()  # nothing leaked back to the host workspace


async def test_host_environment_and_secrets_are_not_visible(
    cache: Path, ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PD_HOST_SECRET", "super-secret-value")
    monkeypatch.setenv("GOOGLE_API_KEY", "another-secret")
    r = await probe(cache, ws, "import os; print(sorted(os.environ))")
    assert "super-secret-value" not in r.output_excerpt and "PD_HOST_SECRET" not in r.output_excerpt
    assert "GOOGLE_API_KEY" not in r.output_excerpt


async def test_the_docker_socket_and_host_files_are_absent(cache: Path, ws: Path) -> None:
    r = await probe(
        cache,
        ws,
        "import os; print(os.path.exists('/var/run/docker.sock'), os.path.exists('/Users'), os.path.exists('/host'))",
    )
    assert "False False False" in r.output_excerpt


async def test_memory_limit_kills_a_memory_hog(cache: Path, ws: Path) -> None:
    r = await probe(
        cache,
        ws,
        "x = bytearray(900 * 1024 * 1024); x[::4096] = b'1' * len(x[::4096]); print('survived')",
        memory="128m",
    )
    assert "survived" not in r.output_excerpt and r.exit_code not in (0, None)


async def test_process_limit_stops_a_fork_bomb(cache: Path, ws: Path) -> None:
    code = (
        "import subprocess\nprocs = []\n"
        "try:\n    for _ in range(300): procs.append(subprocess.Popen(['sleep', '5']))\n"
        "except OSError as e:\n    print('limited at', len(procs))\n"
        "for p in procs: p.kill()\n"
    )
    r = await probe(cache, ws, code, pids=30)
    assert "limited at" in r.output_excerpt
    assert int(r.output_excerpt.split("limited at")[1].split()[0]) < 40


async def test_a_runaway_test_is_killed_and_the_container_is_removed(cache: Path, ws: Path) -> None:
    before = set(containers())
    r = await probe(cache, ws, "import time; time.sleep(120)", test_timeout_s=3)
    assert r.timed_out and r.exit_code is None and not r.passed and r.duration_s < 30
    assert set(containers()) == before  # nothing left running or stopped


async def test_containers_are_destroyed_after_a_normal_run(cache: Path, ws: Path) -> None:
    before = set(containers())
    await probe(cache, ws, "print('hello')")
    assert set(containers()) == before


async def test_output_is_bounded(cache: Path, ws: Path) -> None:
    r = await probe(cache, ws, "print('x' * 5_000_000)", max_output_bytes=10_000)
    assert len(r.output_excerpt) < 4_200


async def test_install_failure_is_a_sandbox_error(cache: Path) -> None:
    with pytest.raises(SandboxError, match="dependency install failed"):
        await sandbox(cache).prepare("pd-this-package-does-not-exist-xyz==9.9.9\n")


async def test_real_pytest_run_end_to_end_baseline_then_fix(cache: Path, ws: Path) -> None:
    """Install pytest from the index (network), then run offline: fail first, pass after the fix."""
    (ws / "app").mkdir()
    (ws / "app" / "__init__.py").write_text("")
    (ws / "app" / "pricing.py").write_text("def apply_discount(p, r):\n    return p * r\n")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_pricing.py").write_text(
        "from app.pricing import apply_discount\n\n\ndef test_discount():\n    assert apply_discount(100.0, 0.1) == 90.0\n\n\n"
        "def test_unrelated():\n    assert 1 + 1 == 2\n"
    )
    sb = sandbox(cache)
    deps = await sb.prepare("pytest==9.2.0\n")
    assert (await sb.prepare("pytest==9.2.0\n")) == deps  # cached the second time

    baseline = await sb.run_tests(ws, deps)
    assert baseline.reproduces_failure and baseline.tests.failed_tests == [
        "tests.test_pricing::test_discount"
    ]
    assert baseline.tests.passed == 1 and not baseline.passed

    (ws / "app" / "pricing.py").write_text("def apply_discount(p, r):\n    return p * (1 - r)\n")
    after = await sb.run_tests(ws, deps)
    assert after.passed and after.tests.total == 2
    assert judge(baseline, after).ok

    # "fixing" by deleting the failing test must be rejected by the judge
    (ws / "app" / "pricing.py").write_text("def apply_discount(p, r):\n    return p * r\n")
    (ws / "tests" / "test_pricing.py").write_text("def test_unrelated():\n    assert True\n")
    cheated = await sb.run_tests(ws, deps)
    assert cheated.passed and not judge(baseline, cheated).ok  # green, but a test disappeared


async def test_zero_tests_collected_is_not_a_pass(cache: Path, ws: Path) -> None:
    sb = sandbox(cache)
    deps = await sb.prepare("pytest==9.2.0\n")
    (ws / "empty.py").write_text("x = 1\n")
    r = await sb.run_tests(ws, deps)
    assert r.exit_code == 5 and not r.passed  # pytest: "no tests collected"
