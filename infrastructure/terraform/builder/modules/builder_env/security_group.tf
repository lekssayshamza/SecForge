resource "aws_security_group" "builder" {
  name        = "builder-${var.builder_id}-SG"
  description = "Builder SG for ${var.builder_id}"
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH from CyberRange VPN"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.vpn_cidr]
  }

  dynamic "ingress" {
    for_each = { for r in var.exposed_ports : "${r.protocol}-${r.from_port}-${r.to_port}" => r
    if !(r.protocol == "tcp" && r.from_port == 22 && r.to_port == 22) }
    content {
      description = "SecForge room ports v1"
      from_port   = ingress.value.from_port
      to_port     = ingress.value.to_port
      protocol    = ingress.value.protocol
      cidr_blocks = [var.vpn_cidr]
    }
  }

  # Runtime synchronization owns subsequent room-port edits. Preserve manual
  # and management ingress on refresh/apply; Terraform still destroys the SG.
  lifecycle {
    ignore_changes = [ingress]
  }

  dynamic "ingress" {
    for_each = var.management_security_group_id == "" ? [] : [var.management_security_group_id]
    content {
      description     = "Private SSH from SecForge application server"
      from_port       = 22
      to_port         = 22
      protocol        = "tcp"
      security_groups = [ingress.value]
    }
  }

  ingress {
    description = "ICMP from CyberRange VPN"
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = [var.vpn_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, local.ownership_tags, { Name = "builder-${var.builder_id}-SG" })
}
