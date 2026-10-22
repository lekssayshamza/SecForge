terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
provider "aws" { region = "eu-north-1" }

# Apply this isolated module only after gateway backups and the fail-closed
# builder-only firewall policy are installed. Never apply the shared VPC.
resource "aws_route" "builder_internet" {
  route_table_id         = var.route_table_id
  destination_cidr_block = "0.0.0.0/0"
  network_interface_id   = var.gateway_network_interface_id
}
resource "aws_vpc_security_group_ingress_rule" "builder_http" {
  security_group_id = var.gateway_group_id
  cidr_ipv4         = var.private_cidr
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  description       = "SecForge builder egress: gateway firewall requires registered creator source"
}
resource "aws_vpc_security_group_ingress_rule" "builder_https" {
  security_group_id = var.gateway_group_id
  cidr_ipv4         = var.private_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "SecForge builder egress: gateway firewall requires registered creator source"
}
output "http_rule_id" { value = aws_vpc_security_group_ingress_rule.builder_http.security_group_rule_id }
output "https_rule_id" { value = aws_vpc_security_group_ingress_rule.builder_https.security_group_rule_id }
