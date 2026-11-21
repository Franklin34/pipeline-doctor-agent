# One service account per workload, each with only what it uses. No project-level roles.

resource "google_service_account" "ingress" {
  account_id   = "${var.name}-ingress"
  display_name = "Pipeline Doctor webhook ingress"
}

resource "google_service_account" "worker" {
  account_id   = "${var.name}-worker"
  display_name = "Pipeline Doctor worker"
}

# Ingress: publish to the events topic, nothing else.
resource "google_pubsub_topic_iam_member" "ingress_publishes" {
  topic  = google_pubsub_topic.events.id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.ingress.email}"
}

# Worker: pull from its subscription, and write to the dead-letter topic (malformed events).
resource "google_pubsub_subscription_iam_member" "worker_pulls" {
  subscription = google_pubsub_subscription.worker.id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_pubsub_topic_iam_member" "worker_dead_letters" {
  topic  = google_pubsub_topic.dead_letter.id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.worker.email}"
}

# Secrets: each workload reads only its own.
resource "google_secret_manager_secret_iam_member" "ingress_webhook_secret" {
  secret_id = google_secret_manager_secret.webhook_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ingress.email}"
}

resource "google_secret_manager_secret_iam_member" "ingress_redis_url" {
  secret_id = google_secret_manager_secret.redis_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ingress.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_redis_url" {
  secret_id = google_secret_manager_secret.redis_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_secrets" {
  for_each  = local.worker_secrets
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}
