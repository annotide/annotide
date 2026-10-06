variable "prefix" {
  description = "Prefix applied to every resource name."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,9}$", var.prefix))
    error_message = "Prefix must be 2-10 lowercase alphanumeric characters, starting with a letter."
  }
}

variable "region" {
  description = "AWS region. EU regions keep data in the EU."
  type        = string
  default     = "eu-north-1"
}

variable "tags" {
  description = "Tags on every resource (the provider's default_tags)."
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

variable "vpc_cidr" {
  description = "CIDR of the VPC. Three private /20s (nodes, pods, PostgreSQL, Redis) and three public /24s (load balancers, NAT) are carved from it."
  type        = string
  default     = "10.40.0.0/16"
}

variable "storage_allowed_ip_ranges" {
  description = <<-EOT
    Public IPs or CIDRs whose browsers may load media from the buckets
    (ARC-3), e.g. office egress IPs. Empty = any address (presigned URLs
    still required). The cluster always gets in through the VPC endpoint.
  EOT
  type        = list(string)
  default     = []
}

variable "api_authorized_ip_ranges" {
  description = "CIDRs allowed to reach the EKS API server's public endpoint. Empty = unrestricted (set this in production)."
  type        = list(string)
  default     = []
}

# --- Sizes -----------------------------------------------------------------

variable "kubernetes_version" {
  description = "EKS Kubernetes version, e.g. \"1.33\"; null = EKS's default."
  type        = string
  default     = null
}

variable "node_instance_types" {
  description = "Instance types of the system node group (x86_64)."
  type        = list(string)
  default     = ["m6i.xlarge"]
}

variable "node_count" {
  description = "Minimum and maximum nodes of the node group. EKS does not scale between them by itself: see README.md, \"Node autoscaling\"."
  type = object({
    min = number
    max = number
  })
  default = { min = 2, max = 5 }
}

variable "postgres_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.m7g.large"
}

variable "postgres_storage_gb" {
  description = "RDS storage in GB; it grows on its own up to four times this."
  type        = number
  default     = 64
}

variable "postgres_multi_az" {
  description = "Standby in a second availability zone for PostgreSQL."
  type        = bool
  default     = false
}

variable "redis_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.t4g.medium"
}

variable "redis_node_count" {
  description = "ElastiCache nodes: 1 = no replica; 2 or more = a replica with automatic failover across zones."
  type        = number
  default     = 2

  validation {
    condition     = var.redis_node_count >= 1 && var.redis_node_count <= 6
    error_message = "redis_node_count must be between 1 and 6."
  }
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

variable "cluster_admin_arns" {
  description = "IAM role or user ARNs (people's SSO roles, the CI role) that administer the cluster with kubectl and helm. Whoever runs terraform gets no access otherwise."
  type        = set(string)
  default     = []
}

# --- Budget ----------------------------------------------------------------

variable "budget_amount" {
  description = "Monthly budget in USD for resources tagged app=annotide; 0 = no budget."
  type        = number
  default     = 0
}

variable "budget_contact_emails" {
  description = "Who gets the budget alerts (50 % forecast, 80 % and 100 % actual)."
  type        = list(string)
  default     = []
}
