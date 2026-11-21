output "ingress_url" {
  description = "Point the Jenkins notification webhook at <this>/webhooks/jenkins."
  value       = google_cloud_run_v2_service.ingress.uri
}

output "events_topic" {
  value = google_pubsub_topic.events.name
}

output "dead_letter_subscription" {
  description = "Read this subscription to see dead-lettered events."
  value       = google_pubsub_subscription.dead_letter_reader.name
}

output "secrets_to_fill" {
  description = "Add a version to each with: gcloud secrets versions add NAME --data-file=-"
  value = [
    google_secret_manager_secret.webhook_secret.secret_id,
    google_secret_manager_secret.github_token.secret_id,
    google_secret_manager_secret.gemini_api_key.secret_id,
  ]
}
