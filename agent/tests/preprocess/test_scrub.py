from __future__ import annotations

import pytest
from pipeline_doctor_agent.preprocess import scrub

# Fake secrets are assembled at runtime so scanners do not flag this file.
GH = "ghp_" + "A1b2C3d4" * 5
GH_PAT = "github_pat_" + "x1Y2z3" * 6
AWS = "AKIA" + "ABCDEFGH12345678"
SLACK = "xoxb-" + "1234567890-" + "abcdefghij"
GOOGLE = "AIza" + "Sy" + "A" * 33
STRIPE = "sk_live_" + "a1b2c3d4e5f6g7h8"
JWT = "eyJ" + "hbGciOiJIUzI1" + ".eyJ" + "zdWIiOiIxMjM0" + ".SflKxwRJSMeKKF2QT4"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabc123\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("secret", "text", "kind"),
    [
        (GH, f"cloning with {GH} now", "github_token"),
        (GH_PAT, f"token {GH_PAT}", "github_token"),
        (AWS, f"key id {AWS} used", "aws_access_key"),
        (SLACK, f"notify {SLACK}", "slack_token"),
        (GOOGLE, f"url?key={GOOGLE}", "google_api_key"),
        (STRIPE, f"charge using {STRIPE}", "payment_key"),
        (JWT, f"session {JWT} expired", "jwt"),
        ("MIIEowIBAAKCAQEA", f"before\n{PEM}\nafter", "private_key"),
        (
            "s3cr3tpassw0rd",
            "postgres://admin:s3cr3tpassw0rd@db.internal:5432/app",
            "url_credentials",
        ),
        ("abcDEF123456", "Authorization: Bearer abcDEF123456xyz", "auth_header"),
        (
            "hunter2hunter2",
            "Server=db;User Id=sa;Password=hunter2hunter2;Encrypt=true",
            "named_secret",
        ),
        ("topsecretvalue", 'config = {"api_key": "topsecretvalue"}', "named_secret"),
        ("topsecretvalue", "export DB_PASSWORD=topsecretvalue", "named_secret"),
        ("Zm9vYmFy", "AccountKey=Zm9vYmFy;EndpointSuffix=core.windows.net", "named_secret"),
        ("supersecret99", "client_secret: supersecret99", "named_secret"),
    ],
)
def test_secret_is_removed_and_counted(secret: str, text: str, kind: str) -> None:
    result = scrub(text)
    assert secret not in result.text
    assert result.counts.get(kind, 0) >= 1
    assert "[REDACTED" in result.text


def test_surrounding_text_is_preserved() -> None:
    result = scrub("before DB_PASSWORD=hunter2hunter2 after")
    assert result.text == "before DB_PASSWORD=[REDACTED:secret] after"


def test_url_keeps_scheme_and_host_for_diagnosis() -> None:
    result = scrub("connecting to postgres://admin:pw123456@db.internal:5432/app")
    assert result.text == "connecting to postgres://[REDACTED:url_credentials]@db.internal:5432/app"


def test_multiple_secrets_on_one_line() -> None:
    result = scrub(f"a={GH} b={AWS} PASSWORD=pw123456")
    assert GH not in result.text and AWS not in result.text and "pw123456" not in result.text
    assert sum(result.counts.values()) >= 3


def test_private_key_unterminated_is_still_removed() -> None:
    result = scrub("log\n-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkq\nmore")
    assert "MIIBVQIBADANBgkq" not in result.text


@pytest.mark.parametrize(
    "benign",
    [
        "Checking out Revision c3d4e5f60718293a4b5c6d7e8f90123456789012 (refs/remotes/origin/main)",
        "tests/test_pricing.py:6: AssertionError",
        "E       assert 10.0 == 90.0",
        "FAILED tests/test_pricing.py::test_apply_discount - assert 10.0 == 90.0",
        'File "/app/pricing.py", line 3, in apply_discount',
        'psycopg2.OperationalError: connection to server at "postgres" (172.18.0.3), port 5432 failed',
        "KeyError: 'name'",
        "Started by user dev1",
        "Successfully installed pytest-8.3.0",
    ],
)
def test_ordinary_log_lines_are_untouched(benign: str) -> None:
    result = scrub(benign)
    assert result.text == benign
    assert result.counts == {}


def test_idempotent() -> None:
    text = f"DB_PASSWORD=hunter2hunter2 {GH} postgres://u:pw123456@h/x\n{PEM}"
    once = scrub(text).text
    assert scrub(once).text == once


def test_counts_never_contain_values() -> None:
    result = scrub(f"token={GH}")
    assert GH not in repr(result.counts)
