"""Logs are attacker-controlled. Pre-processing must stay fast on adversarial input (ReDoS)."""

from __future__ import annotations

import time

import pytest
from pipeline_doctor_agent.preprocess import preprocess
from pipeline_doctor_agent.preprocess.pipeline import MAX_LOG_CHARS

N = 40_000
HOSTILE = {
    "dotted words": "a." * N,
    "dashes": "a-" * N,
    "long word": "a" * (2 * N),
    "repeated keyword": "password." * (N // 5),
    "token equals": "token=" * N,
    "jwt starts": "eyJ" * N,
    "scheme-like": "a.b." * N,
    "url userinfo": "http://" + "a:" * N,
    "unterminated pem": "-----BEGIN PRIVATE KEY-----\n" + "A" * (2 * N),
    "many pem starts": "-----BEGIN PRIVATE KEY-----\n" * (N // 10),
    "exception-like": "Error." * N,
    "huge single line": "ERROR " + "x" * (10 * N),
    "many short lines": "ERROR x\n" * N,
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_preprocess_is_fast_on_hostile_input(name: str) -> None:
    start = time.perf_counter()
    preprocess(HOSTILE[name])
    elapsed = time.perf_counter() - start
    # Real cost is tens of ms; the generous bound only catches quadratic blow-ups, not CI noise.
    assert elapsed < 2.0, f"{name} took {elapsed:.2f}s"


def test_oversized_log_is_truncated_with_warning_keeping_the_tail() -> None:
    log = "start\n" + "filler\n" * (MAX_LOG_CHARS // 4) + "ValueError: the real failure\n"
    r = preprocess(log)
    assert any("truncated" in w for w in r.warnings)
    assert r.error_type == "ValueError"
