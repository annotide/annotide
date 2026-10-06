terraform {
  required_version = ">= 1.5"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.8"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# The subscription comes from ARM_SUBSCRIPTION_ID (or `az account set`).
provider "azurerm" {
  # Shared keys are off on the storage account, so the provider uses Entra ID
  # for any storage data-plane call too.
  storage_use_azuread = true

  features {}
}
