"""Pattern-based secret redaction.

Runs BEFORE anything else touches a log, so secrets cannot reach extraction,
fingerprints, prompts, tickets or traces. Bias: over-redact. A false positive
costs a little context; a miss leaks a credential.

Known limit: patterns catch well-known formats and ``name=value`` pairs with
secret-looking names. A bare high-entropy string with no label is not caught.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class ScrubResult:
    text: str
    counts: dict[str, int]  # kind -> number of redactions; never the secret values


def _tag(kind: str) -> str:
    return f"[REDACTED:{kind}]"


# (kind, pattern, replacement template). Order matters: specific before generic.
# No unbounded prefix: only the VALUE is replaced, so the label never needs capturing, and
# an unbounded `[A-Za-z0-9_.-]*` before the keyword is quadratic on inputs like "a.a.a.a...".
# The suffix is bounded for the same reason (e.g. "password.password.password...").
_SENSITIVE_LABEL = (
    r"(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|apikey|"
    r"access[_-]?key|account[_-]?key|shared[_-]?access[_-]?key|private[_-]?key|"
    r"client[_-]?secret|credential)[A-Za-z0-9_.-]{0,40}"
)

_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        _tag("private_key"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _tag("aws_access_key")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{20,255})\b"),
        _tag("github_token"),
    ),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), _tag("slack_token")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), _tag("google_api_key")),
    (
        "payment_key",
        re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b"),
        _tag("payment_key"),
    ),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,4096}\.eyJ[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}\b"
        ),
        _tag("jwt"),
    ),
    (
        "auth_header",
        re.compile(
            r"(?i)(\bauthorization\s*[:=]\s*)(?:bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,4096}"
        ),
        r"\1" + _tag("auth_header"),
    ),
    (
        "bearer",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,4096}"),
        _tag("bearer"),
    ),
    (
        "url_credentials",  # scheme://user:password@host -> scheme://[REDACTED]@host
        re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]{0,29}://)[^/\s:@]{1,200}:[^/\s@]{1,500}@"),
        r"\1" + _tag("url_credentials") + "@",
    ),
    (
        "named_secret",  # PASSWORD=..., "api_key": "...", Password=...; (connection strings)
        re.compile(
            rf"(?i)({_SENSITIVE_LABEL}\"?[ \t]{{0,5}}[:=][ \t]{{0,5}})"
            rf"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;&\"'\[]+)"
        ),
        r"\1" + _tag("secret"),
    ),
]


def scrub(text: str) -> ScrubResult:
    """Redact secrets. Idempotent: ``scrub(scrub(x).text).text == scrub(x).text``."""
    counts: Counter[str] = Counter()
    for kind, pattern, repl in _RULES:
        text, n = pattern.subn(repl, text)
        if n:
            counts[kind] += n
    return ScrubResult(text=text, counts=dict(counts))
