output "resource_group_name" {
  value = azurerm_resource_group.main.name
}

output "resource_group_id" {
  value = azurerm_resource_group.main.id
}

output "vnet_id" {
  value = azurerm_virtual_network.main.id
}

output "public_subnet_name" {
  value = azurerm_subnet.public.name
}

output "private_subnet_name" {
  value = azurerm_subnet.private.name
}

output "endpoints_subnet_id" {
  value = azurerm_subnet.endpoints.id
}
