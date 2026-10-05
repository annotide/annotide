variable "prefix" {
  description = "Prefix applied to every resource name."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,9}$", var.prefix))
    error_message = "Prefix must be 2-10 lowercase alphanumeric characters, starting with a letter."
  }
}

variable "location" {
  description = "Azure region. EU regions keep data in the EU."
  type        = string
  default     = "swedencentral"
}

variable "tags" {
  description = "Tags on every resource."
  type        = map(string)
  default     = {}
}

variable "public_url" {
  description = "Where people open the app, e.g. https://annotate.example.com. Sets storage CORS and the Helm publicUrl."
  type        = string

  validation {
    condition     = can(regex("^https://[^/]+$", var.public_url))
    error_message = "public_url must be https://host with no path or trailing slash."
  }
}

# --- Network (SEC-1) -------------------------------------------------------

variable "vnet_address_space" {
  description = "Address space of the VNet."
  type        = string
  default     = "10.40.0.0/16"
}

variable "subnet_cidrs" {
  description = "Subnets: AKS nodes."
  type = object({
    aks = string
  })
  default = {
    aks = "10.40.0.0/20"
  }
}

variable "storage_allowed_ip_ranges" {
  description = <<-EOT
    Public IPs or CIDRs whose browsers may load media from storage (ARC-3),
    e.g. office egress IPs. Empty = any address (signed URLs still required).
    Storage does not accept /31 or /32: give single addresses without a prefix.
  EOT
  type        = list(string)
  default     = []
}

variable "admin_ip_ranges" {
  description = "CIDRs of the people who manage Key Vault secrets and reach PostgreSQL directly (backups, psql), e.g. [\"203.0.113.7/32\"]. Empty = only the cluster."
  type        = list(string)
  default     = []
}

variable "api_authorized_ip_ranges" {
  description = "CIDRs allowed to reach the AKS API server. Empty = unrestricted (set this in production)."
  type        = list(string)
  default     = []
}

# --- Sizes -----------------------------------------------------------------

variable "kubernetes_version" {
  description = "AKS Kubernetes version; null = the region's default."
  type        = string
  default     = null
}

variable "node_vm_size" {
  description = "VM size of the AKS system node pool."
  type        = string
  default     = "Standard_D4s_v5"
}

variable "node_count" {
  description = "Minimum and maximum nodes (cluster autoscaler)."
  type = object({
    min = number
    max = number
  })
  default = { min = 2, max = 5 }
}

variable "ingress_enabled" {
  description = "Turn on AKS application routing (managed NGINX) so the chart's Ingress gets a public HTTPS endpoint."
  type        = bool
  default     = true
}

variable "postgres_sku" {
  description = "PostgreSQL Flexible Server SKU."
  type        = string
  default     = "GP_Standard_D2ds_v5"
}

variable "postgres_storage_mb" {
  description = "PostgreSQL storage in MB."
  type        = number
  default     = 65536
}

variable "postgres_high_availability" {
  description = "Zone-redundant standby for PostgreSQL."
  type        = bool
  default     = false
}

variable "redis_sku" {
  description = "Azure Cache for Redis SKU name and capacity."
  type = object({
    name     = string
    family   = string
    capacity = number
  })
  default = { name = "Standard", family = "C", capacity = 1 }
}

variable "storage_replication" {
  description = "Storage replication (LRS, ZRS, GRS, GZRS)."
  type        = string
  default     = "ZRS"
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

variable "cluster_admin_object_ids" {
  description = "Entra object ids (people, groups or the CI principal) that administer the cluster with kubectl and helm."
  type        = set(string)
  default     = []
}

variable "postgres_admins" {
  description = "People or groups who sign in to PostgreSQL with Entra (backups, psql), keyed by user principal name or group name, e.g. { \"alice@example.com\" = { object_id = \"…\", principal_type = \"User\" } }."
  type = map(object({
    object_id      = string
    principal_type = string
  }))
  default = {}

  validation {
    condition     = alltrue([for admin in values(var.postgres_admins) : contains(["User", "Group", "ServicePrincipal"], admin.principal_type)])
    error_message = "principal_type must be User, Group or ServicePrincipal."
  }
}

variable "key_vault_admin_object_ids" {
  description = "Entra object ids (people or the CI principal) that manage Key Vault secrets."
  type        = set(string)
  default     = []
}

# --- Budget ----------------------------------------------------------------

variable "budget_amount" {
  description = "Monthly budget for the resource group in the billing currency; 0 = no budget."
  type        = number
  default     = 0
}

variable "budget_contact_emails" {
  description = "Who gets the budget alerts (50 % forecast, 80 % and 100 % actual)."
  type        = list(string)
  default     = []
}
