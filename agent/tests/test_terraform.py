"""Guard rails on the Terraform: the properties that must not regress even though it is never applied in tests.

`terraform validate` (in CI and `make tf-validate`) checks syntax and provider schema; these tests check
the security-relevant choices a validator cannot see.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TF = Path(__file__).resolve().parents[2] / "deploy" / "terraform"
FILES = {p.name: p.read_text() for p in sorted(TF.glob("*.tf"))}
ALL = "\n".join(FILES.values())


def test_the_terraform_exists_and_covers_the_pieces() -> None:
    assert {
        "pubsub.tf",
        "iam.tf",
        "secrets.tf",
        "cloud_run.tf",
        "redis.tf",
        "monitoring.tf",
    } <= set(FILES)


def test_no_broad_primitive_or_admin_roles() -> None:
    roles = re.findall(r'role\s*=\s*"([^"]+)"', ALL)
    assert roles, "expected explicit IAM roles"
    for r in roles:
        assert r not in {"roles/owner", "roles/editor", "roles/viewer"}, r
        assert not r.endswith(("Admin", ".admin")), r


def test_iam_is_granted_on_resources_not_the_project() -> None:
    assert "google_project_iam" not in ALL  # no project-level bindings at all


def test_the_only_public_grant_is_the_webhook_invoker() -> None:
    hits = re.findall(r"allUsers|allAuthenticatedUsers", ALL)
    assert hits == ["allUsers"]
    block = re.search(
        r'resource "google_cloud_run_v2_service_iam_member" "ingress_public" \{.*?\n\}', ALL, re.S
    )
    assert block and "allUsers" in block.group(0) and "roles/run.invoker" in block.group(0)


def test_the_worker_takes_no_public_traffic() -> None:
    worker = re.search(r'resource "google_cloud_run_v2_service" "worker" \{.*?\n\}\n', ALL, re.S)
    assert worker and 'ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"' in worker.group(0)


def test_every_service_has_its_own_service_account_and_deletion_protection() -> None:
    for name in ("ingress", "worker"):
        block = re.search(
            rf'resource "google_cloud_run_v2_service" "{name}" \{{.*?\n\}}\n', ALL, re.S
        )
        assert block, name
        assert f"google_service_account.{name}.email" in block.group(0)
        assert "deletion_protection = true" in block.group(0)
    assert "compute@developer" not in ALL  # never the default compute account


def test_secret_values_are_never_in_terraform() -> None:
    assert ALL.count("secret_data") == 1  # only the Redis URL, which Terraform itself creates
    assert "google_secret_manager_secret_version" in FILES["secrets.tf"]
    for pattern in (
        r"AIza[0-9A-Za-z_-]{20,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"github_pat_",
        r"-----BEGIN",
        r"ATATT",
    ):
        assert not re.search(pattern, ALL), pattern


def test_secrets_reach_services_only_by_reference() -> None:
    for var in ("WEBHOOK_SHARED_SECRET", "GITHUB_TOKEN", "GOOGLE_API_KEY", "REDIS_URL"):
        block = re.search(rf'name = "{var}"\s*\n\s*value_source \{{\s*secret_key_ref', ALL)
        assert block, f"{var} must come from Secret Manager, not a literal"


def test_each_workload_can_read_only_its_own_secrets() -> None:
    iam = FILES["iam.tf"]
    ingress = re.findall(r'google_secret_manager_secret_iam_member" "(ingress_\w+)"', iam)
    assert sorted(ingress) == [
        "ingress_redis_url",
        "ingress_webhook_secret",
    ]  # not the GitHub token or model key
    assert "worker_secrets" in iam and "webhook_secret" not in re.search(
        r'"worker_secrets".*?\n\}', iam, re.S
    ).group(0)  # type: ignore[union-attr]


def test_automatic_fixes_stay_off_in_the_cloud_worker() -> None:
    assert re.search(r'name\s*=\s*"FIX_ENABLED"\s*\n\s*value\s*=\s*"false"', ALL)


def test_retries_and_the_dead_letter_policy_are_consistent() -> None:
    assert "dead_letter_policy" in FILES["pubsub.tf"]
    assert re.search(
        r'name\s*=\s*"MAX_ATTEMPTS"\s*\n\s*value\s*=\s*tostring\(var\.max_delivery_attempts\)', ALL
    )
    assert re.search(r"max_delivery_attempts\s*=\s*var\.max_delivery_attempts", FILES["pubsub.tf"])
    # a dead-letter topic with no subscription silently drops messages
    assert 'resource "google_pubsub_subscription" "dead_letter_reader"' in FILES["pubsub.tf"]
    assert (
        "gcp-sa-pubsub.iam.gserviceaccount.com" in FILES["pubsub.tf"]
    )  # the service agent that forwards dead letters


def test_the_dead_letter_and_backlog_alerts_exist() -> None:
    m = FILES["monitoring.tf"]
    assert "num_undelivered_messages" in m and "oldest_unacked_message_age" in m


def test_state_and_variable_files_are_ignored_by_git() -> None:
    ignore = (TF.parents[1] / ".gitignore").read_text()
    for entry in (".terraform/", "*.tfstate", "*.tfvars", "!*.tfvars.example"):
        assert entry in ignore
    assert (TF / ".terraform.lock.hcl").exists()  # provider versions are pinned and committed


@pytest.mark.parametrize("name", ["terraform.tfvars.example"])
def test_the_example_has_no_real_project(name: str) -> None:
    assert "your-dedicated-project" in (TF / name).read_text()
