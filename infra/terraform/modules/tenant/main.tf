terraform {
  required_providers {
    databricks = {
      source = "databricks/databricks"
    }
  }
}

locals {
  # Regex replace to sanitize tenant_id for catalog name
  slug         = lower(replace(var.tenant_id, "/[^a-zA-Z0-9_]/", "_"))
  catalog_name = "assarium_${var.environment}_${local.slug}"
}

# The Databricks provider block must be passed in by the root module, configured for the workspace

resource "databricks_catalog" "tenant" {
  name    = local.catalog_name
  comment = "Catalog for tenant ${var.tenant_id}"
}

resource "databricks_schema" "bronze" {
  catalog_name = databricks_catalog.tenant.id
  name         = "bronze"
}

resource "databricks_schema" "silver" {
  catalog_name = databricks_catalog.tenant.id
  name         = "silver"
}

resource "databricks_schema" "gold" {
  catalog_name = databricks_catalog.tenant.id
  name         = "gold"
}

resource "databricks_schema" "assarium" {
  catalog_name = databricks_catalog.tenant.id
  name         = "assarium"
  comment      = "Assarium platform metadata and operational state"
}
