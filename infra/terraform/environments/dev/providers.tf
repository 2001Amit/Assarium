terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.100.0"
    }
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.40.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 2.48.0"
    }
  }
}

provider "azurerm" {
  features {}
  use_oidc = true
}

provider "azuread" {
  use_oidc = true
}

provider "databricks" {
  alias = "account"
  host  = "https://accounts.azuredatabricks.net"
  # Authentication will use Azure CLI or OIDC via Entra ID
}

# The workspace provider will be configured in the root module
# after the workspace is created, or instantiated via aliases.
