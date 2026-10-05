# Azure infrastructure for the annotation platform's Helm chart (§15, SEC-1).
#
# One resource group with a VNet; AKS runs the chart. No paid networking: no
# private endpoints, private DNS zones or NAT gateway. Storage and Key Vault
# admit the AKS subnet through free service endpoints; PostgreSQL and Redis
# admit the cluster's outbound IP through their firewalls. TLS everywhere. The
# cluster's pods sign in to storage, Key Vault, PostgreSQL and Redis with a
# workload identity: no storage keys, database or Redis passwords, or client
# secrets exist anywhere. See README.md for the apply / install steps.

data "azurerm_client_config" "current" {}

locals {
  name            = var.prefix
  service_account = var.helm_release_name
  # Storage account and Key Vault names are global, 3-24 chars, no dashes.
  storage_name = substr("${var.prefix}media${random_string.suffix.result}", 0, 24)
  vault_name   = substr("${var.prefix}-kv-${random_string.suffix.result}", 0, 24)
  tags         = merge({ app = "annotide" }, var.tags)
  # Overlay pod CIDR (the AKS default, spelled out): the ingress controller
  # reaches the backend from it, so the chart trusts its X-Forwarded-For.
  pod_cidr = "10.244.0.0/16"
}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

resource "azurerm_resource_group" "this" {
  name     = "${local.name}-annotation-rg"
  location = var.location
  tags     = local.tags
}

resource "azurerm_log_analytics_workspace" "this" {
  name                = "${local.name}-annotation-logs"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}

# --- Network ---------------------------------------------------------------

resource "azurerm_virtual_network" "this" {
  name                = "${local.name}-annotation-vnet"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  address_space       = [var.vnet_address_space]
  tags                = local.tags
}

resource "azurerm_subnet" "aks" {
  name                 = "aks"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.subnet_cidrs.aks]
  # Free service endpoints: storage and Key Vault firewalls admit this subnet.
  service_endpoints = ["Microsoft.Storage", "Microsoft.KeyVault"]
}

# --- Workload identity ----------------------------------------------------

resource "azurerm_user_assigned_identity" "workload" {
  name                = "${local.name}-annotation-workload"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "workload" {
  name                = "${local.name}-annotation-aks"
  resource_group_name = azurerm_resource_group.this.name
  parent_id           = azurerm_user_assigned_identity.workload.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject             = "system:serviceaccount:${var.kubernetes_namespace}:${local.service_account}"
}

# --- AKS -------------------------------------------------------------------

# The API server is restricted by var.api_authorized_ip_ranges; it stays open
# only when that is left empty, which README.md tells production not to do.
#trivy:ignore:AVD-AZU-0041
resource "azurerm_kubernetes_cluster" "this" {
  name                      = "${local.name}-annotation-aks"
  location                  = azurerm_resource_group.this.location
  resource_group_name       = azurerm_resource_group.this.name
  dns_prefix                = "${local.name}-annotation"
  kubernetes_version        = var.kubernetes_version
  oidc_issuer_enabled       = true
  workload_identity_enabled = true
  azure_policy_enabled      = true
  # Entra ID sign-in and Azure RBAC for kubectl; no static admin credential.
  role_based_access_control_enabled = true
  local_account_disabled            = true
  tags                              = local.tags

  # Application routing: AKS's managed NGINX ingress controller (ingress
  # class webapprouting.kubernetes.azure.com), free as an add-on. Its public
  # IP is the one networking cost of an internet-facing install. Certificates
  # come from cert-manager (README.md, "HTTPS").
  dynamic "web_app_routing" {
    for_each = var.ingress_enabled ? [1] : []
    content {
      dns_zone_ids = []
    }
  }

  azure_active_directory_role_based_access_control {
    azure_rbac_enabled = true
    tenant_id          = data.azurerm_client_config.current.tenant_id
  }

  default_node_pool {
    name                 = "system"
    vm_size              = var.node_vm_size
    vnet_subnet_id       = azurerm_subnet.aks.id
    auto_scaling_enabled = true
    min_count            = var.node_count.min
    max_count            = var.node_count.max
    zones                = ["1", "2", "3"]

    upgrade_settings {
      max_surge = "33%"
    }
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    pod_cidr            = local.pod_cidr
    network_data_plane  = "cilium"
    network_policy      = "cilium"
    load_balancer_sku   = "standard"

    # One managed outbound IP (the one AKS creates anyway): the PostgreSQL and
    # Redis firewalls admit exactly this address.
    load_balancer_profile {
      managed_outbound_ip_count = 1
    }
  }

  dynamic "api_server_access_profile" {
    for_each = length(var.api_authorized_ip_ranges) > 0 ? [1] : []
    content {
      authorized_ip_ranges = var.api_authorized_ip_ranges
    }
  }

  # The chart's worker ScaledObject (OPS-5) needs KEDA.
  workload_autoscaler_profile {
    keda_enabled = true
  }

  oms_agent {
    log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id
  }
}

resource "azurerm_role_assignment" "cluster_admins" {
  for_each             = var.cluster_admin_object_ids
  scope                = azurerm_kubernetes_cluster.this.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = each.value
}

# The cluster manages load balancers in its own subnet.
resource "azurerm_role_assignment" "aks_subnet" {
  scope                = azurerm_subnet.aks.id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_kubernetes_cluster.this.identity[0].principal_id
}

# The cluster's outbound address, for the PostgreSQL and Redis firewalls
# (neither supports service endpoints).
locals {
  egress_ip_id = one(azurerm_kubernetes_cluster.this.network_profile[0].load_balancer_profile[0].effective_outbound_ips)
}

data "azurerm_public_ip" "egress" {
  name                = element(split("/", local.egress_ip_id), 8)
  resource_group_name = element(split("/", local.egress_ip_id), 4)
}

locals {
  egress_ip = data.azurerm_public_ip.egress.ip_address
  # Firewall rules take start / end addresses, not CIDRs.
  admin_ranges = {
    for i, cidr in var.admin_ip_ranges : "admin_${i}" => {
      start = cidrhost(cidr, 0)
      end   = cidrhost(cidr, -1)
    }
  }
}

# --- PostgreSQL (TLS required; firewall: cluster + admins; Entra only) -----

resource "azurerm_postgresql_flexible_server" "this" {
  name                          = "${local.name}-annotation-pg-${random_string.suffix.result}"
  location                      = azurerm_resource_group.this.location
  resource_group_name           = azurerm_resource_group.this.name
  version                       = "16"
  sku_name                      = var.postgres_sku
  storage_mb                    = var.postgres_storage_mb
  backup_retention_days         = 14
  public_network_access_enabled = true
  zone                          = "1"
  tags                          = local.tags

  # Entra sign-in only: the server faces the internet, so no password exists
  # to leak. The app signs in with a token (APP_DATABASE_AUTH=entra).
  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = data.azurerm_client_config.current.tenant_id
  }

  dynamic "high_availability" {
    for_each = var.postgres_high_availability ? [1] : []
    content {
      mode                      = "ZoneRedundant"
      standby_availability_zone = "2"
    }
  }
}

resource "azurerm_postgresql_flexible_server_database" "annotation" {
  name      = "annotation"
  server_id = azurerm_postgresql_flexible_server.this.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# The migrations create these; Flexible Server only allows listed extensions.
resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.this.id
  value     = "CITEXT,PGCRYPTO"
}

# The workload identity owns the database: it runs the migrations. Its role
# name is the identity's name, which the database URL carries.
resource "azurerm_postgresql_flexible_server_active_directory_administrator" "workload" {
  server_name         = azurerm_postgresql_flexible_server.this.name
  resource_group_name = azurerm_resource_group.this.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = azurerm_user_assigned_identity.workload.principal_id
  principal_name      = azurerm_user_assigned_identity.workload.name
  principal_type      = "ServicePrincipal"
}

# People who take backups or use psql, with an `az` token (docs/OPERATIONS.md).
resource "azurerm_postgresql_flexible_server_active_directory_administrator" "admins" {
  for_each            = var.postgres_admins
  server_name         = azurerm_postgresql_flexible_server.this.name
  resource_group_name = azurerm_resource_group.this.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = each.value.object_id
  principal_name      = each.key
  principal_type      = each.value.principal_type

  # The server takes one administrator change at a time.
  depends_on = [azurerm_postgresql_flexible_server_active_directory_administrator.workload]
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "this" {
  for_each         = merge({ cluster = { start = local.egress_ip, end = local.egress_ip } }, local.admin_ranges)
  name             = each.key
  server_id        = azurerm_postgresql_flexible_server.this.id
  start_ip_address = each.value.start
  end_ip_address   = each.value.end
}

# --- Redis (TLS only; firewall: cluster; Entra only) -----------------------

resource "azurerm_redis_cache" "this" {
  name                          = "${local.name}-annotation-redis-${random_string.suffix.result}"
  location                      = azurerm_resource_group.this.location
  resource_group_name           = azurerm_resource_group.this.name
  sku_name                      = var.redis_sku.name
  family                        = var.redis_sku.family
  capacity                      = var.redis_sku.capacity
  non_ssl_port_enabled          = false
  minimum_tls_version           = "1.2"
  public_network_access_enabled = true
  # Entra sign-in only, no access keys (APP_REDIS_AUTH=entra).
  access_keys_authentication_enabled = false
  tags                               = local.tags

  redis_configuration {
    active_directory_authentication_enabled = true
  }
}

# "Data Owner", not "Data Contributor": the worker's start-up runs INFO, which
# Contributor excludes with the rest of @dangerous. The identity is the
# cache's only user.
resource "azurerm_redis_cache_access_policy_assignment" "workload" {
  name               = "workload"
  redis_cache_id     = azurerm_redis_cache.this.id
  access_policy_name = "Data Owner"
  object_id          = azurerm_user_assigned_identity.workload.principal_id
  object_id_alias    = "workload"
}

resource "azurerm_redis_firewall_rule" "cluster" {
  name                = "cluster"
  redis_cache_name    = azurerm_redis_cache.this.name
  resource_group_name = azurerm_resource_group.this.name
  start_ip            = local.egress_ip
  end_ip              = local.egress_ip
}

# --- Media storage ---------------------------------------------------------

# Annotators' browsers load media on signed URLs (ARC-3); with
# storage_allowed_ip_ranges empty that is from anywhere, by design. Every
# request still needs a signed, expiring user-delegation SAS.
#trivy:ignore:AVD-AZU-0012
resource "azurerm_storage_account" "media" {
  name                            = local.storage_name
  location                        = azurerm_resource_group.this.location
  resource_group_name             = azurerm_resource_group.this.name
  account_tier                    = "Standard"
  account_replication_type        = var.storage_replication
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  # Pods use their workload identity (user delegation SAS); no account keys.
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  public_network_access_enabled   = true
  tags                            = local.tags

  # Browsers fetch media straight from storage (ARC-3), so the public
  # endpoint is open to storage_allowed_ip_ranges (anyone when empty; signed
  # URLs are still required). The cluster comes in over its service endpoint.
  network_rules {
    default_action             = length(var.storage_allowed_ip_ranges) == 0 ? "Allow" : "Deny"
    ip_rules                   = var.storage_allowed_ip_ranges
    virtual_network_subnet_ids = [azurerm_subnet.aks.id]
    bypass                     = ["AzureServices"]
  }

  blob_properties {
    versioning_enabled = true

    delete_retention_policy {
      days = 14
    }

    container_delete_retention_policy {
      days = 14
    }

    # The browser reads and uploads media straight from storage (ARC-3, SRC-7).
    cors_rule {
      allowed_origins    = [var.public_url]
      allowed_methods    = ["GET", "HEAD", "PUT", "OPTIONS"]
      allowed_headers    = ["*"]
      exposed_headers    = ["*"]
      max_age_in_seconds = 3600
    }
  }
}

resource "azurerm_storage_container" "this" {
  for_each              = toset(["media", "results", "cache"])
  name                  = each.value
  storage_account_id    = azurerm_storage_account.media.id
  container_access_type = "private"
}

resource "azurerm_role_assignment" "workload_blob" {
  scope                = azurerm_storage_account.media.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
}

# --- Key Vault (secret references, AUTH-7) ---------------------------------

resource "azurerm_key_vault" "this" {
  name                          = local.vault_name
  location                      = azurerm_resource_group.this.location
  resource_group_name           = azurerm_resource_group.this.name
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 30
  public_network_access_enabled = true
  tags                          = local.tags

  # The cluster over its service endpoint; people managing secrets from
  # admin_ip_ranges. Everyone else is denied.
  network_acls {
    default_action             = "Deny"
    bypass                     = "AzureServices"
    ip_rules                   = var.admin_ip_ranges
    virtual_network_subnet_ids = [azurerm_subnet.aks.id]
  }
}

# Connectors and models resolve `azurekeyvault://` references with this.
resource "azurerm_role_assignment" "workload_vault" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
}

resource "azurerm_role_assignment" "vault_admins" {
  for_each             = var.key_vault_admin_object_ids
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = each.value
}

# --- Application secrets ---------------------------------------------------

resource "random_password" "secret_key" {
  length  = 64
  special = false
}

locals {
  # No passwords: the app signs in as the workload identity with a token.
  database_url = format(
    "postgresql+asyncpg://%s@%s:5432/%s?ssl=require",
    urlencode(azurerm_user_assigned_identity.workload.name),
    azurerm_postgresql_flexible_server.this.fqdn,
    azurerm_postgresql_flexible_server_database.annotation.name,
  )
  redis_url = format(
    "rediss://%s:%d/0",
    azurerm_redis_cache.this.hostname,
    azurerm_redis_cache.this.ssl_port,
  )
}

# --- Budget ----------------------------------------------------------------

resource "azurerm_consumption_budget_resource_group" "this" {
  count             = var.budget_amount > 0 ? 1 : 0
  name              = "${local.name}-annotation-budget"
  resource_group_id = azurerm_resource_group.this.id
  amount            = var.budget_amount
  time_grain        = "Monthly"

  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())
  }

  notification {
    enabled        = true
    threshold      = 50
    threshold_type = "Forecasted"
    operator       = "GreaterThan"
    contact_emails = var.budget_contact_emails
  }

  notification {
    enabled        = true
    threshold      = 80
    threshold_type = "Actual"
    operator       = "GreaterThan"
    contact_emails = var.budget_contact_emails
  }

  notification {
    enabled        = true
    threshold      = 100
    threshold_type = "Actual"
    operator       = "GreaterThan"
    contact_emails = var.budget_contact_emails
  }

  lifecycle {
    ignore_changes = [time_period]
  }
}
