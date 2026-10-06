output "gke_name" {
  description = "GKE cluster name."
  value       = google_container_cluster.this.name
}

output "get_credentials_command" {
  description = "Points kubectl and helm at the cluster."
  value       = "gcloud container clusters get-credentials ${google_container_cluster.this.name} --region ${var.region} --project ${var.project_id}"
}

output "bucket_names" {
  description = "Media buckets: media, results, cache."
  value       = { for key, bucket in google_storage_bucket.this : key => bucket.name }
}

output "secrets_prefix" {
  description = "Secret Manager secret ids the workload may read, for gcpsecrets:// references."
  value       = local.secrets_prefix
}

output "postgres_private_ip" {
  description = "Cloud SQL address; reachable from inside the VPC only."
  value       = google_sql_database_instance.this.private_ip_address
}

output "nat_public_ip" {
  description = "The cluster's outbound address, for allow-lists on model endpoints and webhook receivers."
  value       = google_compute_address.nat.address
}

output "workload_service_account" {
  description = "Google service account the pods sign in as (Workload Identity)."
  value       = google_service_account.workload.email
}

output "connector_config" {
  description = "Connector to register in the app for the media bucket (identity managed_identity, no secret)."
  value = {
    type          = "gcs"
    identity_type = "managed_identity"
    config = {
      bucket        = google_storage_bucket.this["media"].name
      project       = var.project_id
      identity_type = "managed_identity"
    }
  }
}

output "helm_values" {
  description = "Values for the annotide chart: terraform output -raw helm_values > values.gcp.yaml"
  sensitive   = true
  value = yamlencode({
    publicUrl = var.public_url
    database  = { url = local.database_url }
    redis     = { url = local.redis_url }
    secrets   = { secretKey = random_password.secret_key.result }
    config = {
      # The ingress controller's pods: their X-Forwarded-For carries the
      # client address for the audit log and rate limits (SEC-3).
      APP_TRUSTED_PROXIES = var.subnet_cidrs.pods
    }
    serviceAccount = {
      create = true
      name   = var.helm_release_name
      annotations = {
        "iam.gke.io/gcp-service-account" = google_service_account.workload.email
      }
    }
  })
}
