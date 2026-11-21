locals {
  apis = [
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "secretmanager.googleapis.com",
    "redis.googleapis.com",
    "monitoring.googleapis.com",
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}
