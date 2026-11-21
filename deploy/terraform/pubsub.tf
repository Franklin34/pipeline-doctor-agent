# Events flow: ingress -> topic -> subscription -> worker. A message that keeps failing is forwarded
# by Pub/Sub to the dead-letter topic, where a subscription keeps it until a human deals with it.

resource "google_pubsub_topic" "events" {
  name                       = "${var.name}-failed"
  message_retention_duration = "604800s" # 7 days: events survive a long worker outage
  depends_on                 = [google_project_service.apis]
}

resource "google_pubsub_topic" "dead_letter" {
  name       = "${var.name}-failed-dlq"
  depends_on = [google_project_service.apis]
}

# A dead-letter topic without a subscription DROPS messages. This subscription retains them.
resource "google_pubsub_subscription" "dead_letter_reader" {
  name                       = "${var.name}-dlq-reader"
  topic                      = google_pubsub_topic.dead_letter.id
  message_retention_duration = "1209600s" # 14 days
  retain_acked_messages      = false
  expiration_policy {
    ttl = "" # never expires
  }
}

resource "google_pubsub_subscription" "worker" {
  name  = "${var.name}-worker"
  topic = google_pubsub_topic.events.id

  # The worker extends the deadline while it works, so this only bounds how soon a crashed
  # worker's message returns.
  ack_deadline_seconds       = 60
  message_retention_duration = "604800s"

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = var.max_delivery_attempts
  }

  expiration_policy {
    ttl = ""
  }
}

# Pub/Sub's own service agent forwards dead letters, so it needs these two grants.
data "google_project" "this" {
  depends_on = [google_project_service.apis]
}

locals {
  pubsub_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "agent_publishes_dead_letters" {
  topic  = google_pubsub_topic.dead_letter.id
  role   = "roles/pubsub.publisher"
  member = local.pubsub_agent
}

resource "google_pubsub_subscription_iam_member" "agent_acks_forwarded" {
  subscription = google_pubsub_subscription.worker.id
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}
