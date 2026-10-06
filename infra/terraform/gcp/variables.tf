variable "project_id" {
  description = "Google Cloud project that holds everything. Use a project of its own."
  type        = string
}

variable "prefix" {
  description = "Prefix applied to every resource name."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,9}$", var.prefix))
    error_message = "Prefix must be 2-10 lowercase alphanumeric characters, starting with a letter."
  }
}

variable "region" {
  description = "Google Cloud region. EU regions keep data in the EU."
  type        = string
  default     = "europe-north1"
}

variable "labels" {
  description = "Labels on every resource that takes them (lowercase keys and values)."
  type        = map(string)
  default     = {}
}

variable "public_url" {
  description = "Where people open the app, e.g. https://annotate.example.com. Sets bucket CORS and the Helm publicUrl."
  type        = string

  validation {
    condition     = can(regex("^https://[^/]+$", var.public_url))
    error_message = "public_url must be https://host with no path or trailing slash."
  }
}

# --- Network (SEC-1) -------------------------------------------------------

variable "subnet_cidrs" {
  description = "Node subnet and its secondary ranges for pods and services (VPC-native GKE)."
  type = object({
    nodes    = string
    pods     = string
    services = string
  })
  default = {
    nodes    = "10.40.0.0/20"
    pods     = "10.44.0.0/14"
    services = "10.48.0.0/20"
  }
}

variable "api_authorized_ip_ranges" {
  description = "CIDRs allowed to reach the GKE control plane. Empty = unrestricted (set this in production)."
  type        = list(string)
  default     = []
}

# --- Sizes -----------------------------------------------------------------

variable "kubernetes_version" {
  description = "Minimum GKE master version; null = the REGULAR release channel's default."
  type        = string
  default     = null
}

variable "node_machine_type" {
  description = "Machine type of the system node pool."
  type        = string
  default     = "n2-standard-4"
}

variable "node_count" {
  description = "Minimum and maximum nodes across the region's zones (cluster autoscaler)."
  type = object({
    min = number
    max = number
  })
  default = { min = 2, max = 5 }
}

variable "postgres_tier" {
  description = "Cloud SQL machine tier (Enterprise edition)."
  type        = string
  default     = "db-custom-2-8192"
}

variable "postgres_disk_gb" {
  description = "Cloud SQL disk size in GB; it grows on its own."
  type        = number
  default     = 64
}

variable "postgres_high_availability" {
  description = "Regional (zone-redundant) Cloud SQL with a standby."
  type        = bool
  default     = false
}

variable "redis_tier" {
  description = "Memorystore tier: BASIC (one node) or STANDARD_HA (replica with failover)."
  type        = string
  default     = "STANDARD_HA"

  validation {
    condition     = contains(["BASIC", "STANDARD_HA"], var.redis_tier)
    error_message = "redis_tier must be BASIC or STANDARD_HA."
  }
}

variable "redis_memory_gb" {
  description = "Memorystore size in GB."
  type        = number
  default     = 1
}

variable "redis_tls" {
  description = <<-EOT
    TLS to Memorystore. Its certificate is signed by a per-instance Google CA
    that is not in the system trust store, and the app cannot be given a CA
    yet, so turning this on breaks the API and worker until it can. Off,
    Redis is reachable only from the VPC and needs its AUTH string.
  EOT
  type        = bool
  default     = false
}

# --- Identity and the Helm release ----------------------------------------

variable "kubernetes_namespace" {
  description = "Namespace the Helm release goes into."
  type        = string
  default     = "annotation"
}

variable "helm_release_name" {
  description = "Helm release name; the chart's service account is named after it."
  type        = string
  default     = "annotation"
}

# --- Budget ----------------------------------------------------------------

variable "billing_account" {
  description = "Billing account id (XXXXXX-XXXXXX-XXXXXX), for the budget only."
  type        = string
  default     = ""
}

variable "budget_amount" {
  description = "Monthly budget for the project in the billing account's currency; 0 = no budget. Needs billing_account."
  type        = number
  default     = 0
}

variable "budget_contact_emails" {
  description = "Who gets the budget alerts (50 % forecast, 80 % and 100 % actual), besides the billing account's admins."
  type        = list(string)
  default     = []
}
