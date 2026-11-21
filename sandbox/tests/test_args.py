"""The hardening flags, asserted one by one on the exact command line (no Docker needed)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pd_sandbox import Limits, build_install_args, build_test_args

WS, DEPS, RES, REQ = Path("/ws"), Path("/deps"), Path("/results"), Path("/req.txt")
LIMITS = Limits(memory="256m", cpus=0.5, pids=64)


@pytest.fixture
def test_args() -> list[str]:
    return build_test_args("pd-sbx-test-1", "python:3.13-slim", WS, DEPS, RES, LIMITS)


def pair(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_tests_run_without_network_as_nobody_with_everything_locked_down(
    test_args: list[str],
) -> None:
    assert pair(test_args, "--network") == "none"
    assert pair(test_args, "--user") == "65534:65534"
    assert pair(test_args, "--cap-drop") == "ALL"
    assert pair(test_args, "--security-opt") == "no-new-privileges"
    assert "--read-only" in test_args and "--rm" in test_args and "--init" in test_args
    assert (
        pair(test_args, "--memory") == "256m" == pair(test_args, "--memory-swap")
    )  # no swap escape
    assert pair(test_args, "--cpus") == "0.5" and pair(test_args, "--pids-limit") == "64"
    assert "/tmp:rw,nosuid,size=64m" in test_args  # noqa: S108
    assert "--label" in test_args and "pd-sandbox=1" in test_args  # so orphans can be swept
    assert test_args[-1] != "" and test_args[test_args.index("python:3.13-slim") + 1] == "python"


def test_only_the_results_directory_is_writable(test_args: list[str]) -> None:
    mounts = [test_args[i + 1] for i, a in enumerate(test_args) if a == "--mount"]
    assert sorted(m.split(",dst=")[1].split(",")[0] for m in mounts) == [
        "/deps",
        "/results",
        "/work",
    ]
    for m in mounts:
        if ",dst=/results" in m:
            assert not m.endswith(",readonly")
        else:
            assert m.endswith(",readonly"), m  # workspace and dependencies are read-only


def test_no_host_environment_or_secrets_are_passed(test_args: list[str]) -> None:
    envs = [test_args[i + 1] for i, a in enumerate(test_args) if a == "--env"]
    assert all(
        "=" in e for e in envs
    )  # explicit KEY=VALUE only; a bare '--env KEY' would copy the host value
    assert "--env-file" not in test_args
    assert {e.split("=")[0] for e in envs} == {
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "HOME",
        "PIP_DISABLE_PIP_VERSION_CHECK",
        "PYTHONPATH",
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        "--privileged",
        "--cap-add",
        "--pid",
        "--ipc",
        "--device",
        "--volume",
        "-v",
        "--volumes-from",
        "--security-opt=seccomp=unconfined",
        "--userns",
        "--net=host",
        "--publish",
        "-p",
        "--add-host",
    ],
)
def test_dangerous_flags_never_appear(forbidden: str, test_args: list[str]) -> None:
    install = build_install_args("n", "python:3.13-slim", REQ, DEPS, LIMITS)
    for args in (test_args, install):
        docker_flags = args[
            : args.index("python:3.13-slim")
        ]  # after the image comes the container's command
        assert forbidden not in docker_flags


def test_docker_socket_and_host_paths_are_never_mounted(test_args: list[str]) -> None:
    assert not any("docker.sock" in a or a in {"/", "/etc", "/root"} for a in test_args)


def test_install_step_has_network_but_nothing_else_and_only_wheels() -> None:
    args = build_install_args("pd-sbx-install-1", "python:3.13-slim", REQ, DEPS, LIMITS)
    assert "--network" not in args  # default bridge: pip must reach the index
    assert "--only-binary=:all:" in args  # an sdist's setup.py would be arbitrary code with network
    assert (
        pair(args, "--user") == "65534:65534"
        and pair(args, "--cap-drop") == "ALL"
        and "--read-only" in args
    )
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert any(",dst=/req/requirements.txt,readonly" in m for m in mounts)
    assert not any(
        "/work" in m or "ws" in m for m in mounts
    )  # the repository is never mounted here


def test_the_container_name_is_set_so_a_timeout_can_kill_it(test_args: list[str]) -> None:
    assert pair(test_args, "--name") == "pd-sbx-test-1"
