# Google Cloud infrastructure for the annotation platform's Helm chart (§15,
# SEC-1).
#
# One VPC in one region; a regional GKE cluster runs the chart. Nodes have no
# public address, and Cloud SQL and Memorystore have private addresses only
# (private services access, free). The only paid networking is Cloud NAT for
# outbound traffic (image pulls, model endpoints) and, once the ingress
# controller is installed, its load balancer. The nodes reach Google APIs
# and Cloud Storage over Private Google Access (free). The pods sign in to
# Cloud Storage and Secret Manager as a Google service account through
# Workload Identity: no service-account keys exist anywhere. See README.md
# for the apply / install steps.

data "google_project" "this" {}

locals {
  name            = "${var.prefix}-annotation"
  service_account = var.helm_release_name
  # Bucket names are global.
  bucket_prefix = "${local.name}-${random_string.suffix.result}"
  # Secret Manager secrets the workload may read, for `gcpsecrets://`
  # references (AUTH-7).
  secrets_prefix = "${local.name}-"
  budget_enabled = var.budget_amount > 0 && var.billing_account != ""
}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

resource "google_project_service" "this" {
  for_each = toset(concat(
    [
      "compute.googleapis.com",
      "container.googleapis.com",
      "iamcredentials.googleapis.com", # signBlob: signed URLs without a key
      "redis.googleapis.com",
      "secretmanager.googleapis.com",
      "servicenetworking.googleapis.com",
      "sqladmin.googleapis.com",
    ],
    local.budget_enabled ? ["billingbudgets.googleapis.com", "monitoring.googleapis.com"] : [],
  ))
  service            = each.value
  disable_on_destroy = false
}

# --- Network ---------------------------------------------------------------

resource "google_compute_network" "this" {
  name                    = "${local.name}-vpc"
  auto_create_subnetworks = false

  depends_on = [google_project_service.this]
}

resource "google_compute_subnetwork" "gke" {
  name                     = "${local.name}-gke"
  region                   = var.region
  network                  = google_compute_network.this.id
  ip_cidr_range            = var.subnet_cidrs.nodes
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.subnet_cidrs.pods
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.subnet_cidrs.services
  }

  log_config {
    aggregation_interval = "INTERVAL_10_MIN"
    flow_sampling        = 0.1
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# One reserved address, so the cluster has one fixed outbound IP (model
# endpoints and webhook receivers can allow-list it).
resource "google_compute_address" "nat" {
  name   = "${local.name}-nat"
  region = var.region
}

resource "google_compute_router" "this" {
  name    = "${local.name}-router"
  region  = var.region
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "this" {
  name                               = "${local.name}-nat"
  router                             = google_compute_router.this.name
  region                             = var.region
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.nat.self_link]
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# Private services access: Cloud SQL and Memorystore get addresses in this
# range, reachable from the VPC only.
resource "google_compute_global_address" "services" {
  name          = "${local.name}-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = google_compute_network.this.id
}

resource "google_service_networking_connection" "this" {
  network                 = google_compute_network.this.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.services.name]
}

# --- Identities ------------------------------------------------------------

# The nodes' own identity: logs and metrics only, never the default compute
# service account (Editor on the project).
resource "google_service_account" "nodes" {
  account_id   = "${var.prefix}-annotation-nodes"
  display_name = "${local.name} GKE nodes"
}

resource "google_project_iam_member" "nodes" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
    "roles/artifactregistry.reader",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.nodes.email}"
}

# What the pods sign in as. A Google service account rather than a bare
# Kubernetes principal: signing a URL needs a service-account email to sign
# with (the GCS connector's managed_identity).
resource "google_service_account" "workload" {
  account_id   = "${var.prefix}-annotation"
  display_name = "${local.name} workload"
}

# Exactly the chart's service account, nothing else in the cluster.
resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.workload.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.kubernetes_namespace}/${local.service_account}]"

  depends_on = [google_container_cluster.this]
}

# Signed URLs are signed through the IAM signBlob API as itself (ARC-3).
resource "google_service_account_iam_member" "workload_signer" {
  service_account_id = google_service_account.workload.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.workload.email}"
}

resource "google_project_iam_member" "workload_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.workload.email}"

  condition {
    title      = "${local.name} secrets"
    expression = "resource.name.startsWith(\"projects/${data.google_project.this.number}/secrets/${local.secrets_prefix}\")"
  }
}

# --- GKE -------------------------------------------------------------------

# The control plane's public endpoint is restricted by
# var.api_authorized_ip_ranges; it stays open only when that is left empty,
# which README.md tells production not to do.
#trivy:ignore:AVD-GCP-0061
resource "google_container_cluster" "this" {
  name                = "${local.name}-gke"
  location            = var.region
  network             = google_compute_network.this.id
  subnetwork          = google_compute_subnetwork.gke.id
  min_master_version  = var.kubernetes_version
  deletion_protection = true

  # Node pools are managed below.
  remove_default_node_pool = true
  initial_node_count       = 1

  release_channel {
    channel = "REGULAR"
  }

  networking_mode = "VPC_NATIVE"
  # Dataplane V2 (Cilium) enforces the chart's NetworkPolicies.
  datapath_provider = "ADVANCED_DATAPATH"

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }

  dynamic "master_authorized_networks_config" {
    for_each = length(var.api_authorized_ip_ranges) > 0 ? [1] : []
    content {
      dynamic "cidr_blocks" {
        for_each = var.api_authorized_ip_ranges
        content {
          cidr_block   = cidr_blocks.value
          display_name = "authorized-${cidr_blocks.key}"
        }
      }
    }
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  enable_shielded_nodes = true

  master_auth {
    client_certificate_config {
      issue_client_certificate = false
    }
  }

  # The default pool exists only while the cluster is created; it gets the
  # same hardening as the real one.
  node_config {
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    metadata        = { disable-legacy-endpoints = "true" }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }

  lifecycle {
    ignore_changes = [node_config, initial_node_count]
  }

  depends_on = [google_project_service.this, google_project_iam_member.nodes]
}

resource "google_container_node_pool" "system" {
  name     = "system"
  cluster  = google_container_cluster.this.id
  location = var.region

  initial_node_count = 1

  autoscaling {
    total_min_node_count = var.node_count.min
    total_max_node_count = var.node_count.max
    location_policy      = "BALANCED"
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }

  node_config {
    machine_type    = var.node_machine_type
    image_type      = "COS_CONTAINERD"
    disk_type       = "pd-balanced"
    disk_size_gb    = 50
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    metadata        = { disable-legacy-endpoints = "true" }

    # Pods see only their own Workload Identity, never the node's.
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }

  # The cluster autoscaler owns the current size.
  lifecycle {
    ignore_changes = [initial_node_count]
  }
}

# --- PostgreSQL (private IP; TLS required) ---------------------------------

# ssl_mode ENCRYPTED_ONLY refuses every unencrypted connection; Trivy accepts
# only client-certificate mode, which the app has no certificate for.
#trivy:ignore:AVD-GCP-0015
resource "google_sql_database_instance" "this" {
  name                = "${local.name}-pg-${random_string.suffix.result}"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = true

  settings {
    # Enterprise Plus (the POSTGRES_16 default) has no custom tiers.
    edition           = "ENTERPRISE"
    tier              = var.postgres_tier
    availability_type = var.postgres_high_availability ? "REGIONAL" : "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = var.postgres_disk_gb
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.this.id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7

      backup_retention_settings {
        retained_backups = 14
      }
    }

    maintenance_window {
      day  = 7
      hour = 3
    }

    insights_config {
      query_insights_enabled = true
    }

    database_flags {
      name  = "log_checkpoints"
      value = "on"
    }

    database_flags {
      name  = "log_connections"
      value = "on"
    }

    database_flags {
      name  = "log_disconnections"
      value = "on"
    }

    database_flags {
      name  = "log_lock_waits"
      value = "on"
    }

    database_flags {
      name  = "log_temp_files"
      value = "0"
    }

    database_flags {
      name  = "log_min_duration_statement"
      value = "-1"
    }
  }

  depends_on = [google_service_networking_connection.this]
}

# citext and pgcrypto are supported on Cloud SQL; the migrations create them.
resource "google_sql_database" "annotation" {
  name     = "annotation"
  instance = google_sql_database_instance.this.name
}

# The app signs in with a password (APP_DATABASE_AUTH=password): it has no
# Cloud SQL IAM sign-in. The database is unreachable from outside the VPC.
resource "random_password" "postgres" {
  length  = 40
  special = false
}

resource "google_sql_user" "annotation" {
  name     = "annotation"
  instance = google_sql_database_instance.this.name
  password = random_password.postgres.result
}

# --- Redis (private IP; AUTH) ----------------------------------------------

resource "google_redis_instance" "this" {
  name                    = "${local.name}-redis"
  region                  = var.region
  tier                    = var.redis_tier
  memory_size_gb          = var.redis_memory_gb
  redis_version           = "REDIS_7_2"
  authorized_network      = google_compute_network.this.id
  connect_mode            = "PRIVATE_SERVICE_ACCESS"
  auth_enabled            = true
  transit_encryption_mode = var.redis_tls ? "SERVER_AUTHENTICATION" : "DISABLED"

  maintenance_policy {
    weekly_maintenance_window {
      day = "SUNDAY"

      start_time {
        hours = 3
      }
    }
  }

  depends_on = [google_service_networking_connection.this]
}

# --- Media storage ---------------------------------------------------------

resource "google_storage_bucket" "this" {
  for_each                    = toset(["media", "results", "cache"])
  name                        = "${local.bucket_prefix}-${each.value}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  # Deleted objects stay recoverable for 14 days; so do overwritten ones.
  soft_delete_policy {
    retention_duration_seconds = 14 * 24 * 3600
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 14
    }
    action {
      type = "Delete"
    }
  }

  # The browser reads and uploads media straight from the bucket (ARC-3,
  # SRC-7).
  cors {
    origin          = [var.public_url]
    method          = ["GET", "HEAD", "PUT", "OPTIONS"]
    response_header = ["Content-Type", "Content-Length", "ETag"]
    max_age_seconds = 3600
  }
}

# Objects, plus reading the bucket's own settings (the connector check reads
# its CORS rules).
resource "google_storage_bucket_iam_member" "workload" {
  for_each = {
    for pair in setproduct(keys(google_storage_bucket.this), ["roles/storage.objectAdmin", "roles/storage.legacyBucketReader"]) :
    "${pair[0]}/${pair[1]}" => { bucket = google_storage_bucket.this[pair[0]].name, role = pair[1] }
  }
  bucket = each.value.bucket
  role   = each.value.role
  member = "serviceAccount:${google_service_account.workload.email}"
}

# --- Application secrets ---------------------------------------------------

resource "random_password" "secret_key" {
  length  = 64
  special = false
}

locals {
  database_url = format(
    "postgresql+asyncpg://%s:%s@%s:5432/%s?ssl=require",
    google_sql_user.annotation.name,
    urlencode(random_password.postgres.result),
    google_sql_database_instance.this.private_ip_address,
    google_sql_database.annotation.name,
  )
  redis_url = format(
    "%s://:%s@%s:%d/0",
    var.redis_tls ? "rediss" : "redis",
    urlencode(google_redis_instance.this.auth_string),
    google_redis_instance.this.host,
    google_redis_instance.this.port,
  )
}

# --- Budget ----------------------------------------------------------------

resource "google_monitoring_notification_channel" "budget" {
  for_each     = local.budget_enabled ? toset(var.budget_contact_emails) : toset([])
  display_name = "${local.name} budget: ${each.value}"
  type         = "email"

  labels = {
    email_address = each.value
  }

  depends_on = [google_project_service.this]
}

resource "google_billing_budget" "this" {
  count           = local.budget_enabled ? 1 : 0
  billing_account = var.billing_account
  display_name    = "${local.name}-budget"

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      units = tostring(var.budget_amount)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "FORECASTED_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.8
  }

  threshold_rules {
    threshold_percent = 1.0
  }

  all_updates_rule {
    monitoring_notification_channels = [for channel in google_monitoring_notification_channel.budget : channel.id]
  }

  depends_on = [google_project_service.this]
}
