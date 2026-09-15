variable "project_name" {
  type        = string
  description = "Base name for the project (e.g., 'assarium')"
}

variable "environment" {
  type        = string
  description = "Environment name (e.g., 'dev', 'prod')"
}

variable "location" {
  type        = string
  description = "Azure region (e.g., 'eastus')"
}

variable "vnet_address_space" {
  type        = string
  description = "CIDR block for the VNet"
  default     = "10.139.0.0/16"
}
