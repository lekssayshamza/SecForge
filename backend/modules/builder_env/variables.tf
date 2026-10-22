variable "builder_id" {
  description = "Unique builder identifier (e.g., bld-42)"
  type        = string
}

variable "creator_id" {
  description = "Creator user ID for tagging"
  type        = number
}

variable "room_id" {
  description = "Room ID for tagging"
  type        = number
}

variable "ami_id" {
  description = "Ubuntu 24.04 LTS AMI ID"
  type        = string
  default     = "ami-0769f265f707fecc8"
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.micro"
}

variable "subnet_id" {
  description = "Private subnet ID in CyberRange VPC"
  type        = string
  default     = "subnet-017557ddd10df5804"
}

variable "vpc_id" {
  description = "CyberRange VPC ID"
  type        = string
  default     = "vpc-0e50eceba146f3a4a"
}

variable "ssh_key_name" {
  description = "SSH key pair name"
  type        = string
  default     = "CyberRange-Key"
}

variable "vpn_cidr" {
  description = "OpenVPN client CIDR"
  type        = string
  default     = "10.8.0.0/24"
  validation {
    condition = can(cidrnetmask(var.vpn_cidr)) && try(
      tonumber(split("/", var.vpn_cidr)[1]) >= 16 && (
        can(regex("^10[.]", cidrhost(var.vpn_cidr, 0))) ||
        can(regex("^172[.](1[6-9]|2[0-9]|3[01])[.]", cidrhost(var.vpn_cidr, 0))) ||
        can(regex("^192[.]168[.]", cidrhost(var.vpn_cidr, 0)))
    ), false)
    error_message = "Use a private IPv4 VPN CIDR of /16 or narrower."
  }
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
variable "exposed_ports" {
  description = "Room services exposed only to the configured VPN; SSH management remains available"
  type = list(object({
    protocol  = string
    from_port = number
    to_port   = number
  }))
  default = [
    { protocol = "tcp", from_port = 22, to_port = 22 },
    { protocol = "tcp", from_port = 80, to_port = 80 }
  ]
  validation {
    condition = length(var.exposed_ports) <= 20 && alltrue([
      for r in var.exposed_ports : contains(["tcp", "udp"], r.protocol) &&
      r.from_port >= 1 && r.to_port <= 65535 && r.from_port <= r.to_port &&
      floor(r.from_port) == r.from_port && floor(r.to_port) == r.to_port
    ]) && sum(concat([0], [for r in var.exposed_ports : r.to_port - r.from_port + 1])) <= 4096
    error_message = "Use at most 20 TCP/UDP rules, valid integer ports 1-65535, and at most 4096 ports."
  }
}
