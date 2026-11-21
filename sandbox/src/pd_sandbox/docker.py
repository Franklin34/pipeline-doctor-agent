"""Run untrusted code (a repository plus a model-written patch) in an ephemeral Docker container.

Two containers per check, never shared:

1. **install** (network ON, only a requirements file mounted): ``pip install --only-binary=:all:``
   into a dependencies directory. Wheels only, so no ``setup.py`` of an sdist ever runs. The
   result is cached by the hash of the requirements and the image.
2. **test** (network OFF): the repository mounted read-only, the dependencies read-only, one
   writable results directory, a read-only root filesystem, a non-root user, no capabilities,
   no secrets, CPU / memory / process / time limits. Destroyed when it exits.

``build_test_args`` / ``build_install_args`` are pure functions so tests can assert every flag.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from pd_sandbox.junit import summarize_junit
from pd_sandbox.models import SandboxError, SandboxResult

LABEL = "pd-sandbox=1"
DEFAULT_IMAGE = "python:3.13-slim"
NOBODY = "65534:65534"
EXCERPT_CHARS = 4_000
DEFAULT_TEST_COMMAND = (
    "python",
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
    "--junitxml=/results/junit.xml",
)


@dataclass(frozen=True)
class Limits:
    memory: str = "512m"
    cpus: float = 1.0
    pids: int = 128
    tmpfs_size: str = "64m"
    test_timeout_s: float = 120.0
    install_timeout_s: float = 300.0
    max_output_bytes: int = 200_000


@dataclass(frozen=True)
class Proc:
    returncode: int | None  # None: killed after a timeout
    output: bytes
    timed_out: bool = False


Exec = Callable[[list[str], float, int], Awaitable[Proc]]


async def run_process(args: list[str], timeout_s: float, max_bytes: int) -> Proc:
    """Run a command, merging stderr into stdout, keeping at most ``max_bytes`` of output."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
    )
    stdout = proc.stdout
    if stdout is None:  # cannot happen with PIPE; keeps the type checker honest without an assert
        raise RuntimeError("no stdout pipe")
    chunks: list[bytes] = []
    kept = 0

    async def drain() -> None:
        nonlocal kept
        while chunk := await stdout.read(65_536):
            if kept < max_bytes:  # keep reading (so the child never blocks) but stop storing
                chunks.append(chunk[: max_bytes - kept])
                kept += len(chunks[-1])

    reader = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        await reader
        return Proc(None, b"".join(chunks), timed_out=True)
    except BaseException:
        proc.kill()
        reader.cancel()
        raise
    await reader
    return Proc(proc.returncode, b"".join(chunks))


# ---- pure argument builders (tested flag by flag) --------------------------------------------------


def _hardening(name: str, limits: Limits) -> list[str]:
    return [
        "--rm",
        "--name",
        name,
        "--label",
        LABEL,
        "--init",
        "--hostname",
        "sandbox",
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,nosuid,size={limits.tmpfs_size}",  # noqa: S108 - inside the container
        "--user",
        NOBODY,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(limits.pids),
        "--memory",
        limits.memory,
        "--memory-swap",
        limits.memory,
        "--cpus",
        str(limits.cpus),
        "--ulimit",
        "nofile=256:256",
        "--ulimit",
        "core=0",
    ]


def _bind(src: Path, dst: str, *, readonly: bool) -> list[str]:
    # --mount (not -v): Docker errors if the source is missing instead of creating a directory.
    return ["--mount", f"type=bind,src={src},dst={dst}" + (",readonly" if readonly else "")]


_ENV = [
    "PYTHONDONTWRITEBYTECODE=1",
    "PYTHONUNBUFFERED=1",
    "HOME=/tmp",
    "PIP_DISABLE_PIP_VERSION_CHECK=1",
]


def build_test_args(
    name: str,
    image: str,
    workspace: Path,
    deps: Path,
    results: Path,
    limits: Limits,
    command: tuple[str, ...] = DEFAULT_TEST_COMMAND,
    docker: str = "docker",
) -> list[str]:
    env = [x for e in [*_ENV, "PYTHONPATH=/deps"] for x in ("--env", e)]
    return [
        docker,
        "run",
        *_hardening(name, limits),
        "--network",
        "none",
        *_bind(workspace, "/work", readonly=True),
        *_bind(deps, "/deps", readonly=True),
        *_bind(results, "/results", readonly=False),  # the only writable mount
        "--workdir",
        "/work",
        *env,
        image,
        *command,
    ]


def build_install_args(
    name: str, image: str, requirements: Path, deps: Path, limits: Limits, docker: str = "docker"
) -> list[str]:
    env = [x for e in _ENV for x in ("--env", e)]
    return [
        docker,
        "run",
        *_hardening(name, limits),  # network stays on: pip must reach the index
        *_bind(requirements, "/req/requirements.txt", readonly=True),
        *_bind(deps, "/deps", readonly=False),
        *env,
        image,
        "python",
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--no-input",
        "--only-binary=:all:",
        "--target",
        "/deps",
        "-r",
        "/req/requirements.txt",
    ]


# ---- the sandbox ---------------------------------------------------------------------------------


class DockerSandbox:
    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        limits: Limits | None = None,
        *,
        exec_: Exec = run_process,
        cache_dir: Path | None = None,
        docker: str = "docker",
    ) -> None:
        self.image, self.limits, self._exec, self.docker = image, limits or Limits(), exec_, docker
        self._cache = cache_dir or Path(tempfile.gettempdir()) / "pd-sandbox-deps"

    # -- dependencies (network on, cached) -----------------------------------------------------
    async def prepare(self, requirements: str | None) -> Path:
        """Install requirements into a directory and return it (empty dir if none)."""
        key = hashlib.sha256(f"{self.image}\n{requirements or ''}".encode()).hexdigest()[:24]
        deps = self._cache / key
        if (deps / ".pd-ok").exists():
            return deps
        shutil.rmtree(deps, ignore_errors=True)
        deps.mkdir(parents=True)
        deps.chmod(0o777)  # the non-root container user must be able to write here
        if requirements and requirements.strip():
            with tempfile.TemporaryDirectory(prefix="pd-req-") as tmp:
                req = Path(tmp) / "requirements.txt"
                req.write_text(requirements)
                req.chmod(0o644)
                name = f"pd-sbx-install-{uuid.uuid4().hex[:8]}"
                proc = await self._docker_run(
                    name,
                    build_install_args(name, self.image, req, deps, self.limits, self.docker),
                    self.limits.install_timeout_s,
                )
            if proc.timed_out or proc.returncode != 0:
                shutil.rmtree(deps, ignore_errors=True)
                raise SandboxError(
                    "dependency install failed: "
                    + _excerpt(proc.output)
                    + (" (timed out)" if proc.timed_out else "")
                )
        _make_readable(deps)
        (deps / ".pd-ok").write_text("ok")
        return deps

    # -- tests (network off) -----------------------------------------------------------------------
    async def run_tests(
        self, workspace: Path, deps: Path, command: tuple[str, ...] = DEFAULT_TEST_COMMAND
    ) -> SandboxResult:
        results = Path(tempfile.mkdtemp(prefix="pd-results-"))
        results.chmod(0o777)
        _make_readable(workspace)
        started = time.monotonic()
        try:
            name = f"pd-sbx-test-{uuid.uuid4().hex[:8]}"
            proc = await self._docker_run(
                name,
                build_test_args(
                    name, self.image, workspace, deps, results, self.limits, command, self.docker
                ),
                self.limits.test_timeout_s,
            )
            junit = results / "junit.xml"
            xml = (
                junit.read_bytes()[:5_000_000]
                if junit.is_file() and not junit.is_symlink()
                else b""
            )
        finally:
            shutil.rmtree(results, ignore_errors=True)
        return SandboxResult(
            exit_code=proc.returncode,
            timed_out=proc.timed_out,
            duration_s=round(time.monotonic() - started, 2),
            tests=summarize_junit(xml),
            output_excerpt=_excerpt(proc.output),
        )

    # -- plumbing -----------------------------------------------------------------------------------
    async def _docker_run(self, name: str, args: list[str], timeout_s: float) -> Proc:
        try:
            proc = await self._exec(args, timeout_s, self.limits.max_output_bytes)
        except FileNotFoundError as exc:
            raise SandboxError("docker is not installed or not on PATH") from exc
        except BaseException:
            await self._remove(name)  # cancelled or crashed: never leave the container behind
            raise
        if proc.timed_out:
            await self._remove(name)  # killing the docker CLI does not stop the container
        elif proc.returncode == 125:  # docker itself failed (daemon down, image missing, bad flag)
            raise SandboxError("docker could not start the container: " + _excerpt(proc.output))
        return proc

    async def _remove(self, name: str) -> None:
        with contextlib.suppress(Exception):
            await asyncio.shield(self._exec([self.docker, "rm", "-f", name], 30.0, 10_000))

    async def cleanup_orphans(self) -> int:
        """Remove sandbox containers left behind by a crashed process."""
        listed = await self._exec(
            [self.docker, "ps", "-aq", "--filter", f"label={LABEL}"], 30.0, 100_000
        )
        ids = listed.output.decode().split()
        if ids:
            await self._exec([self.docker, "rm", "-f", *ids], 60.0, 10_000)
        return len(ids)


def _excerpt(output: bytes) -> str:
    text = output.decode("utf-8", errors="replace")
    return text if len(text) <= EXCERPT_CHARS else "...\n" + text[-EXCERPT_CHARS:]


def _make_readable(root: Path) -> None:
    """The container runs as 'nobody': give it read (and directory traverse) access."""
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames:
            Path(dirpath, n).chmod(Path(dirpath, n).stat().st_mode | 0o555)
        for n in filenames:
            p = Path(dirpath, n)
            if not p.is_symlink():
                p.chmod(p.stat().st_mode | 0o444)
    root.chmod(root.stat().st_mode | 0o555)
