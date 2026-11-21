locals {
  vpc = {
    network    = var.network
    subnetwork = var.subnetwork
  }
}

# ---- webhook ingress: public, because Jenkins must reach it; every request is HMAC-verified --------
resource "google_cloud_run_v2_service" "ingress" {
  name                = "${var.name}-ingress"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true

  template {
    service_account = google_service_account.ingress.email
    timeout         = "15s"

    scaling {
      min_instance_count = 0
      max_instance_count = var.ingress_max_instances
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = local.vpc.network
        subnetwork = local.vpc.subnetwork
      }
    }

    containers {
      image = var.ingress_image
      ports {
        container_port = 8080
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "256Mi"
        }
      }
      env {
        name  = "QUEUE_BACKEND"
        value = "pubsub"
      }
      env {
        name  = "PUBSUB_PROJECT"
        value = var.project_id
      }
      env {
        name  = "PUBSUB_TOPIC"
        value = google_pubsub_topic.events.name
      }
      env {
        name = "WEBHOOK_SHARED_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.webhook_secret.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "REDIS_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.redis_url.secret_id
            version = "latest"
          }
        }
      }
      startup_probe {
        http_get {
          path = "/healthz"
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.ingress_webhook_secret,
    google_secret_manager_secret_iam_member.ingress_redis_url,
    google_secret_manager_secret_version.redis_url,
  ]
}

# The webhook must be callable without Google credentials. Authentication is the HMAC signature,
# checked in code on every request (constant-time, fail closed).
resource "google_cloud_run_v2_service_iam_member" "ingress_public" {
  name     = google_cloud_run_v2_service.ingress.name
  location = google_cloud_run_v2_service.ingress.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---- worker: pulls from Pub/Sub, so it needs CPU all the time and never takes public traffic ----------
resource "google_cloud_run_v2_service" "worker" {
  name                = "${var.name}-worker"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = true

  template {
    service_account                  = google_service_account.worker.email
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 1
      max_instance_count = 3
    }

    vpc_access {
      egress = "ALL_TRAFFIC" # reaches Redis privately and GitHub, Jira and the model API through the VPC's NAT
      network_interfaces {
        network    = local.vpc.network
        subnetwork = local.vpc.subnetwork
      }
    }

    containers {
      image = var.worker_image
      # Cloud Run needs a listening port to call the instance healthy: the metrics endpoint.
      ports {
        container_port = 9102
      }
      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
        cpu_idle = false # the worker pulls in the background; CPU must not be throttled between requests
      }
      env {
        name  = "QUEUE_BACKEND"
        value = "pubsub"
      }
      env {
        name  = "PUBSUB_PROJECT"
        value = var.project_id
      }
      env {
        name  = "PUBSUB_SUBSCRIPTION"
        value = google_pubsub_subscription.worker.name
      }
      env {
        name  = "PUBSUB_DEAD_LETTER_TOPIC"
        value = google_pubsub_topic.dead_letter.name
      }
      env {
        name  = "MAX_ATTEMPTS"
        value = tostring(var.max_delivery_attempts)
      }
      env {
        name  = "METRICS_ADDR"
        value = "0.0.0.0"
      }
      env {
        name  = "METRICS_PORT"
        value = "9102"
      }
      env {
        name  = "FIX_ENABLED"
        value = "false" # automatic fixes need a Docker host for the sandbox; Cloud Run has none
      }
      env {
        name  = "GITHUB_MODE"
        value = "real"
      }
      env {
        name  = "GITHUB_ALLOWED_REPOS"
        value = var.github_allowed_repos
      }
      env {
        name = "REDIS_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.redis_url.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GITHUB_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.github_token.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GOOGLE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_key.secret_id
            version = "latest"
          }
        }
      }
      startup_probe {
        tcp_socket {
          port = 9102
        }
        period_seconds    = 5
        failure_threshold = 12
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.worker_redis_url,
    google_secret_manager_secret_iam_member.worker_secrets,
    google_pubsub_subscription_iam_member.worker_pulls,
    google_secret_manager_secret_version.redis_url,
  ]
}
