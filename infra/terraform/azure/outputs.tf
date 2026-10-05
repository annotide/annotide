output "resource_group_name" {
  description = "The resource group holding everything."
  value       = azurerm_resource_group.this.name
}

output "aks_name" {
  description = "AKS cluster name."
  value       = azurerm_kubernetes_cluster.this.name
}

output "get_credentials_command" {
  description = "Points kubectl and helm at the cluster."
  value       = "az aks get-credentials --resource-group ${azurerm_resource_group.this.name} --name ${azurerm_kubernetes_cluster.this.name}"
}

output "storage_account_name" {
  description = "Media storage account (containers media, results, cache)."
  value       = azurerm_storage_account.media.name
}

output "key_vault_name" {
  description = "Key Vault for connector and model secret references (azurekeyvault://)."
  value       = azurerm_key_vault.this.name
}

output "postgres_fqdn" {
  description = "PostgreSQL host; its firewall admits the cluster and admin_ip_ranges."
  value       = azurerm_postgresql_flexible_server.this.fqdn
}

output "workload_identity_client_id" {
  description = "Client id of the identity the pods sign in to Azure with."
  value       = azurerm_user_assigned_identity.workload.client_id
}

output "connector_config" {
  description = "Connector to register in the app for the media container (identity managed_identity, no secret)."
  value = {
    type          = "azure_blob"
    identity_type = "managed_identity"
    config = {
      account_url   = azurerm_storage_account.media.primary_blob_endpoint
      account_name  = azurerm_storage_account.media.name
      container     = "media"
      identity_type = "managed_identity"
    }
  }
}

output "helm_values" {
  description = "Values for the annotide chart: terraform output -raw helm_values > values.azure.yaml"
  sensitive   = true
  value = yamlencode({
    publicUrl = var.public_url
    database  = { url = local.database_url }
    redis     = { url = local.redis_url }
    secrets   = { secretKey = random_password.secret_key.result }
    config = {
      APP_AZURE_KEY_VAULT_NAME = azurerm_key_vault.this.name
      # Tokens for the workload identity instead of passwords (SEC-1).
      APP_DATABASE_AUTH = "entra"
      APP_REDIS_AUTH    = "entra"
      # The ingress controller's pods: their X-Forwarded-For carries the
      # client address for the audit log and rate limits (SEC-3).
      APP_TRUSTED_PROXIES = local.pod_cidr
    }
    serviceAccount = {
      create = true
      name   = var.helm_release_name
      annotations = {
        "azure.workload.identity/client-id" = azurerm_user_assigned_identity.workload.client_id
      }
    }
    podLabels = {
      "azure.workload.identity/use" = "true"
    }
  })
}
