# Entra ID app registration and groups for trying single sign-on and group
# sync (AUTH-1, AUTH-3) against a real tenant. Apply it yourself; see
# docs/ENTRA.md. Nothing here is used by the application at runtime.

terraform {
  required_version = ">= 1.0"
  required_providers {
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
  }
}

data "azuread_client_config" "current" {}

locals {
  # Microsoft Graph and its delegated OpenID scopes.
  graph_app_id = "00000003-0000-0000-c000-000000000000"
  graph_scopes = {
    openid  = "37f7f235-527c-4136-accd-4a02d197296e"
    profile = "14dad69e-099b-42c9-810b-d002981feec1"
    email   = "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0"
  }
  owners = [data.azuread_client_config.current.object_id]
}

resource "azuread_application" "annotation" {
  display_name     = "${var.prefix}-annotation-sso"
  sign_in_audience = "AzureADMyOrg"
  owners           = local.owners

  # Security groups the user belongs to go into the ID token as object ids.
  # "ApplicationGroup" instead would release only groups assigned to the app,
  # which keeps tokens small in large tenants (and avoids group overage).
  group_membership_claims = [var.group_claim]

  optional_claims {
    # Entra omits `email` unless asked (and then only for mailbox users);
    # the platform falls back to `preferred_username` / `upn`.
    id_token {
      name = "email"
    }
    id_token {
      name = "upn"
    }
  }

  web {
    redirect_uris = [var.redirect_uri]
    logout_url    = var.logout_url

    implicit_grant {
      access_token_issuance_enabled = false
      id_token_issuance_enabled     = false
    }
  }

  required_resource_access {
    resource_app_id = local.graph_app_id

    dynamic "resource_access" {
      for_each = local.graph_scopes
      content {
        id   = resource_access.value
        type = "Scope"
      }
    }
  }
}

resource "azuread_service_principal" "annotation" {
  client_id                    = azuread_application.annotation.client_id
  app_role_assignment_required = false
  owners                       = local.owners
}

resource "azuread_application_password" "annotation" {
  application_id = azuread_application.annotation.id
  display_name   = "annotide"
}

resource "azuread_group" "role" {
  for_each = var.groups

  display_name     = "${var.prefix}-annotation-${each.key}"
  description      = each.value.description
  security_enabled = true
  owners           = local.owners
  members          = each.value.members
}
