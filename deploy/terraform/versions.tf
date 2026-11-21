terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Remote state: create the bucket by hand once (versioned, private), then uncomment and run
  # `terraform init -migrate-state`. State contains the Redis password, so the bucket must be locked down.
  # backend "gcs" {
  #   bucket = "YOUR-STATE-BUCKET"
  #   prefix = "pipeline-doctor"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
