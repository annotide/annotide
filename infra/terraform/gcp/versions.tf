terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# Credentials come from `gcloud auth application-default login` (or
# GOOGLE_APPLICATION_CREDENTIALS in CI).
provider "google" {
  project        = var.project_id
  region         = var.region
  default_labels = merge({ app = "annotide" }, var.labels)

  # The budget API needs a quota project with user credentials.
  billing_project       = var.project_id
  user_project_override = true
}
