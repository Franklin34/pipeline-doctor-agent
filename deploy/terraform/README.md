# deploy/terraform

Google Cloud infrastructure for Pipeline Doctor. **Written and validated, never applied.** `terraform validate` passes and
`agent/tests/test_terraform.py` guards the security choices; nothing here has run against a real project, and no `plan` has been
made (that needs credentials).

## What it creates
| File | Resources |
|---|---|
| `pubsub.tf` | events topic; worker subscription (retry backoff 10-600 s, dead-letter policy); dead-letter topic **plus a subscription that retains it** (a dead-letter topic alone drops messages) |
| `iam.tf` | a service account per workload; only resource-level grants (ingress may publish, worker may pull and dead-letter, each reads only its own secrets); no project-level roles |
| `secrets.tf` | Secret Manager containers. Values are added by hand so the webhook secret, GitHub token and model key never enter Terraform state |
| `redis.tf` | Memorystore (Basic, 1 GB, AUTH on) for idempotency keys and webhook de-duplication |
| `cloud_run.tf` | ingress (public, HMAC-verified in code) and worker (internal only, CPU always on, one request at a time) |
| `monitoring.tf` | dead-letter-not-empty and backlog-age alert policies |

## Use
```bash
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars      # edit; the file is git-ignored
terraform init
terraform plan                                     # needs gcloud application-default credentials
```
Build and push both images to Artifact Registry first (`services/ingress/Dockerfile`, `agent/Dockerfile`; the latter from the repository root). After the first `apply`, add a version to each secret in `secrets_to_fill`.

**Cost:** Memorystore Basic 1 GB is the main fixed cost (tens of USD a month); the worker's minimum of one always-on instance adds more. Delete the stack when not in use.

## Known limits
- **No automatic fixes in the cloud.** The fix sandbox needs Docker; Cloud Run has none, so `FIX_ENABLED=false`. A sandbox runner (a VM or gVisor-based job) is future work.
- **Jenkins and Jira are still the fixtures** in the cloud worker image: there is no real Jenkins backend yet, and no Jira site to test one against. Until then the cloud worker proves the transport, not the integrations.
- **Redis traffic is unencrypted inside the VPC** (AUTH only); the clients would need the server CA for TLS.
- **State holds the Redis password**; use a locked-down, versioned bucket (see `versions.tf`).
- `MAX_ATTEMPTS` on the worker and `max_delivery_attempts` on the subscription must be equal; the variable feeds both.
