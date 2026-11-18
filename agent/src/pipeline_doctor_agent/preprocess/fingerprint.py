"""Failure fingerprints: stable across runs, different for different failures."""

from __future__ import annotations

import hashlib
import re

VERSION = "v1"

_NORMALIZERS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<uuid>",
    ),
    (
        re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "<ts>",
    ),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-f]{12,}\b"), "<hex>"),
    (re.compile(r"/(?:tmp|var/folders)/[^\s'\"]+"), "<tmp>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def normalize_text(text: str) -> str:
    """Strip run-specific noise (ids, times, addresses, numbers) from a message."""
    for pattern, repl in _NORMALIZERS:
        text = pattern.sub(repl, text)
    return text.strip()


def compute_fingerprint(error_type: str | None, location: str, fallback_message: str | None) -> str:
    """16 hex chars of sha256 over version, normalized type and location.

    Line numbers are deliberately not part of the input (they change with every
    edit). When no error type is known, the normalized first error line is used
    so unrelated failures do not all collapse into one fingerprint.
    """
    etype = error_type or f"unknown:{normalize_text(fallback_message or '')}"
    basis = "\n".join([VERSION, etype, normalize_text(location)])
    return hashlib.sha256(basis.encode()).hexdigest()[:16]
