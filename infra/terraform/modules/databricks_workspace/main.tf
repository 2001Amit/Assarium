terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
    }
  }
}

locals {
  prefix = "${var.project_name}-${var.environment}"
}

resource "azurerm_databricks_workspace" "main" {
  name                = "dbw-${local.prefix}"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "premium"
  
  managed_resource_group_name = "rg-${local.prefix}-dbw-managed"

  custom_parameters {
    no_public_ip        = true
    virtual_network_id  = var.vnet_id
    public_subnet_name  = var.public_subnet_name
    private_subnet_name = var.private_subnet_name
  }
  
  tags = {
    Environment = var.environment
    Project     = var.project_name
  }
}
