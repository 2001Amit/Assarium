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

# Core Resource Group
resource "azurerm_resource_group" "main" {
  name     = "rg-${local.prefix}-${var.location}"
  location = var.location
  tags = {
    Environment = var.environment
    Project     = var.project_name
  }
}

# Virtual Network
resource "azurerm_virtual_network" "main" {
  name                = "vnet-${local.prefix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = [var.vnet_address_space]
}

# Network Security Group for Databricks
resource "azurerm_network_security_group" "databricks" {
  name                = "nsg-${local.prefix}-databricks"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

# Databricks Public Subnet (Host)
resource "azurerm_subnet" "public" {
  name                 = "snet-${local.prefix}-public"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [cidrsubnet(var.vnet_address_space, 2, 0)] # 10.139.0.0/18

  delegation {
    name = "databricks-del"
    service_delegation {
      name    = "Microsoft.Databricks/workspaces"
      actions = [
        "Microsoft.Network/virtualNetworks/subnets/join/action",
        "Microsoft.Network/virtualNetworks/subnets/prepareNetworkPolicies/action",
        "Microsoft.Network/virtualNetworks/subnets/unprepareNetworkPolicies/action",
      ]
    }
  }
}

# Databricks Private Subnet (Container)
resource "azurerm_subnet" "private" {
  name                 = "snet-${local.prefix}-private"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [cidrsubnet(var.vnet_address_space, 2, 1)] # 10.139.64.0/18

  delegation {
    name = "databricks-del"
    service_delegation {
      name    = "Microsoft.Databricks/workspaces"
      actions = [
        "Microsoft.Network/virtualNetworks/subnets/join/action",
        "Microsoft.Network/virtualNetworks/subnets/prepareNetworkPolicies/action",
        "Microsoft.Network/virtualNetworks/subnets/unprepareNetworkPolicies/action",
      ]
    }
  }
}

# Subnet to NSG Associations
resource "azurerm_subnet_network_security_group_association" "public" {
  subnet_id                 = azurerm_subnet.public.id
  network_security_group_id = azurerm_network_security_group.databricks.id
}

resource "azurerm_subnet_network_security_group_association" "private" {
  subnet_id                 = azurerm_subnet.private.id
  network_security_group_id = azurerm_network_security_group.databricks.id
}

# Private Endpoints Subnet (for Key Vault, Storage, etc)
resource "azurerm_subnet" "endpoints" {
  name                 = "snet-${local.prefix}-endpoints"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [cidrsubnet(var.vnet_address_space, 8, 128)] # 10.139.128.0/24
}
