output "issuer" {
  description = "APP_OIDC_ISSUER."
  value       = "https://login.microsoftonline.com/${data.azuread_client_config.current.tenant_id}/v2.0"
}

output "client_id" {
  description = "APP_OIDC_CLIENT_ID."
  value       = azuread_application.annotation.client_id
}

output "client_secret" {
  description = "APP_OIDC_CLIENT_SECRET. Store it in your secret manager; it is not shown by default."
  value       = azuread_application_password.annotation.value
  sensitive   = true
}

output "group_object_ids" {
  description = "Object ids to use as keys of a project's settings.idp_groups and in APP_OIDC_ADMIN_GROUPS."
  value       = { for name, group in azuread_group.role : name => group.object_id }
}
