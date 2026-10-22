variable "vpc_id" { type = string }
variable "subnet_id" { type = string }
variable "challenge_subnet" { type = string }
variable "gateway_group" { type = string }
variable "key_name" { type = string }
variable "state_bucket" { type = string }
variable "protected_resources" { type = list(string) }
variable "admin_cidr" {
  type = string
  validation {
    condition     = can(cidrhost(var.admin_cidr, 0)) && endswith(var.admin_cidr, "/32")
    error_message = "Administrator SSH must be one IPv4 /32 address."
  }
}
