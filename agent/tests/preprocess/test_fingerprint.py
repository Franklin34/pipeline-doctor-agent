from __future__ import annotations

import re

from pipeline_doctor_agent.preprocess.fingerprint import compute_fingerprint, normalize_text


def test_format_matches_jira_fingerprint_pattern() -> None:
    assert re.fullmatch(
        r"[a-f0-9]{16}", compute_fingerprint("AssertionError", "tests/x.py::t", None)
    )


def test_deterministic() -> None:
    a = compute_fingerprint("AssertionError", "tests/x.py::t", None)
    assert a == compute_fingerprint("AssertionError", "tests/x.py::t", None)


def test_different_type_or_location_changes_fingerprint() -> None:
    base = compute_fingerprint("AssertionError", "tests/x.py::t", None)
    assert base != compute_fingerprint("ValueError", "tests/x.py::t", None)
    assert base != compute_fingerprint("AssertionError", "tests/x.py::other", None)


def test_unknown_type_uses_normalized_message_but_ignores_noise() -> None:
    a = compute_fingerprint(
        None, "stage:Build", "build 17 failed at 2026-11-12 10:11:12 on 10.0.0.5"
    )
    b = compute_fingerprint(
        None, "stage:Build", "build 18 failed at 2026-11-13 09:00:00 on 10.0.0.9"
    )
    c = compute_fingerprint(None, "stage:Build", "disk is full")
    assert a == b and a != c


def test_normalize_text() -> None:
    out = normalize_text(
        "id 3f2b8a1c-0000-4000-8000-123456789abc at 172.18.0.3:5432 ptr 0xdeadbeef took 12.5s /tmp/pytest-of-x/y"
    )
    assert out == "id <uuid> at <ip> ptr <hex> took <n>s <tmp>"
