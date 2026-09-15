variable "project_name" {
  type    = string
  default = "assarium"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "location" {
  type    = string
  default = "eastus"
}

# Principal IDs for Databricks admin and users
variable "admin_principal_id" {
  type        = string
  description = "Object ID of the Entra ID group or user for Admins"
}
