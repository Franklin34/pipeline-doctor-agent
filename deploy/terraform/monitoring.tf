# Cloud Monitoring alerts (the Pub/Sub equivalents of the Prometheus rules in deploy/observability).
# The worker cannot read queue depth from Pub/Sub the way it can from Redis Streams, so these use
# Google's own subscription metrics.

resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  display_name = "Pipeline Doctor alerts"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

locals {
  channels = [for c in google_monitoring_notification_channel.email : c.id]
}

resource "google_monitoring_alert_policy" "dead_letter_not_empty" {
  display_name = "Pipeline Doctor: dead-letter queue is not empty"
  combiner     = "OR"
  conditions {
    display_name = "Undelivered dead letters"
    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND resource.labels.subscription_id = \"${google_pubsub_subscription.dead_letter_reader.name}\" AND metric.type = \"pubsub.googleapis.com/subscription/num_undelivered_messages\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  notification_channels = local.channels
  documentation {
    content   = "A failed pipeline got no diagnosis and no ticket. Read the messages on the dead-letter subscription; see docs/runbook.md."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "backlog_age" {
  display_name = "Pipeline Doctor: events are waiting too long"
  combiner     = "OR"
  conditions {
    display_name = "Oldest unacked message over 10 minutes"
    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND resource.labels.subscription_id = \"${google_pubsub_subscription.worker.name}\" AND metric.type = \"pubsub.googleapis.com/subscription/oldest_unacked_message_age\""
      comparison      = "COMPARISON_GT"
      threshold_value = 600
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  notification_channels = local.channels
  documentation {
    content   = "The worker is stalled, down or overloaded. Check the Cloud Run worker service's logs and instance count."
    mime_type = "text/markdown"
  }
}
