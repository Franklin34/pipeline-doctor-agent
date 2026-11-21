# Idempotency keys, leases and locks (pd:*) and webhook de-duplication live in Redis, even when the
# queue itself is Pub/Sub. Basic tier (no replica) is fine: losing it only makes duplicate
# suppression forget recent events, and every external write is idempotent anyway.

resource "google_redis_instance" "state" {
  name               = "${var.name}-state"
  tier               = "BASIC"
  memory_size_gb     = 1
  region             = var.region
  redis_version      = "REDIS_7_2"
  authorized_network = "projects/${var.project_id}/global/networks/${var.network}"
  auth_enabled       = true
  # In-transit TLS would need the server CA in both clients. Traffic stays inside the VPC; see the
  # threat model's residual risks before running this outside a dedicated project.
  transit_encryption_mode = "DISABLED"
  depends_on              = [google_project_service.apis]
}
