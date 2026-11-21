# Secret *containers* only. Values are added by a person (`gcloud secrets versions add ...`), so the
# webhook secret, the GitHub token and the Gemini key never pass through Terraform or its state.
# The one exception is the Redis URL, which Terraform itself creates from the Memorystore instance.

resource "google_secret_manager_secret" "webhook_secret" {
  secret_id = "${var.name}-webhook-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "github_token" {
  secret_id = "${var.name}-github-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "${var.name}-gemini-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "redis_url" {
  secret_id = "${var.name}-redis-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "redis_url" {
  secret      = google_secret_manager_secret.redis_url.id
  secret_data = "redis://:${google_redis_instance.state.auth_string}@${google_redis_instance.state.host}:${google_redis_instance.state.port}/0"
}

locals {
  worker_secrets = {
    github_token   = google_secret_manager_secret.github_token
    gemini_api_key = google_secret_manager_secret.gemini_api_key
  }
}
