variable "route_table_id" { type = string }
variable "gateway_network_interface_id" { type = string }
variable "gateway_group_id" { type = string }
variable "private_cidr" {
  type = string
  validation {
    condition     = can(cidrnetmask(var.private_cidr)) && var.private_cidr != "0.0.0.0/0"
    error_message = "An explicitly verified private IPv4 subnet is required."
  }
}
