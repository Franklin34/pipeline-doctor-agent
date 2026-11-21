variable "project_id" {
  description = "GCP project to deploy into. Use a dedicated project."
  type        = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "name" {
  description = "Prefix for resource names."
  type        = string
  default     = "pipeline-doctor"
}

variable "ingress_image" {
  description = "Container image of the webhook ingress (Artifact Registry), pinned by digest or tag."
  type        = string
}

variable "worker_image" {
  description = "Container image of the Python worker."
  type        = string
}

variable "network" {
  description = "VPC network name for Memorystore and Cloud Run direct egress."
  type        = string
  default     = "default"
}

variable "subnetwork" {
  description = "Subnetwork in var.region for Cloud Run direct VPC egress."
  type        = string
  default     = "default"
}

variable "max_delivery_attempts" {
  description = "Deliveries before Pub/Sub dead-letters a message (5-100). MUST equal the worker's MAX_ATTEMPTS."
  type        = number
  default     = 5

  validation {
    condition     = var.max_delivery_attempts >= 5 && var.max_delivery_attempts <= 100
    error_message = "Pub/Sub allows 5 to 100 delivery attempts."
  }
}

variable "alert_email" {
  description = "Email for alert notifications. Empty creates the alert policies without a channel."
  type        = string
  default     = ""
}

variable "ingress_max_instances" {
  type    = number
  default = 5
}

variable "github_allowed_repos" {
  description = "Comma-separated owner/name list the worker may reach."
  type        = string
  default     = ""
}
