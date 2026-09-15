terraform {
  required_providers {
    databricks = {
      source = "databricks/databricks"
      # This module uses the 'account' provider alias for UC setup
      configuration_aliases = [databricks.account]
    }
    azurerm = {
      source = "hashicorp/azurerm"
    }
  }
}

locals {
  prefix = "${var.project_name}-${var.environment}"
}

# Unity Catalog storage account
resource "azurerm_storage_account" "uc" {
  name                     = "stuc${var.project_name}${var.environment}"
  resource_group_name      = var.resource_group_name
  location                 = var.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  is_hns_enabled           = true
}

resource "azurerm_storage_container" "uc" {
  name                  = "metastore"
  storage_account_name  = azurerm_storage_account.uc.name
  container_access_type = "private"
}

# Storage credential (links Databricks Access Connector to UC Storage)
resource "databricks_storage_credential" "uc" {
  provider = databricks.account
  name     = "cred-${local.prefix}"
  azure_managed_identity {
    access_connector_id = var.access_connector_id
  }
}

# The external location
resource "databricks_external_location" "uc" {
  provider        = databricks.account
  name            = "ext-${local.prefix}"
  url             = "abfss://${azurerm_storage_container.uc.name}@${azurerm_storage_account.uc.name}.dfs.core.windows.net/"
  credential_name = databricks_storage_credential.uc.id
  comment         = "Managed by Terraform"
}

# The Metastore
resource "databricks_metastore" "main" {
  provider      = databricks.account
  name          = "meta-${local.prefix}"
  storage_root  = "abfss://${azurerm_storage_container.uc.name}@${azurerm_storage_account.uc.name}.dfs.core.windows.net/"
  region        = var.location
  force_destroy = true
}

# Assign workspace to metastore
resource "databricks_metastore_assignment" "main" {
  provider             = databricks.account
  metastore_id         = databricks_metastore.main.id
  workspace_id         = parseint(element(split("/", var.workspace_id), length(split("/", var.workspace_id)) - 1), 10) # Extract workspace ID from resource ID
  default_catalog_name = "hive_metastore"
}
