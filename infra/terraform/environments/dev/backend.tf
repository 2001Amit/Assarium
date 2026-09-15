terraform {
  backend "azurerm" {
    resource_group_name  = "rg-assarium-tfstate"
    storage_account_name = "stassariumtfstatedev"
    container_name       = "tfstate"
    key                  = "dev.terraform.tfstate"
    use_oidc             = true
  }
}
