module "landing_zone" {
  source = "../../modules/azure_landing_zone"

  project_name = var.project_name
  environment  = var.environment
  location     = var.location
}

module "workspace" {
  source = "../../modules/databricks_workspace"

  project_name        = var.project_name
  environment         = var.environment
  location            = var.location
  resource_group_name = module.landing_zone.resource_group_name
  vnet_id             = module.landing_zone.vnet_id
  public_subnet_name  = module.landing_zone.public_subnet_name
  private_subnet_name = module.landing_zone.private_subnet_name
}

# The Access Connector for Unity Catalog
resource "azurerm_databricks_access_connector" "uc" {
  name                = "ext-${var.project_name}-${var.environment}"
  resource_group_name = module.landing_zone.resource_group_name
  location            = var.location

  identity {
    type = "SystemAssigned"
  }
}

module "unity_catalog" {
  source = "../../modules/unity_catalog"

  project_name        = var.project_name
  environment         = var.environment
  location            = var.location
  resource_group_name = module.landing_zone.resource_group_name
  workspace_id        = module.workspace.workspace_id
  access_connector_id = azurerm_databricks_access_connector.uc.id
}
