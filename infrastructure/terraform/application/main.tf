terraform {
  required_version = ">= 1.10.0"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 6.0" } }
}
provider "aws" { region = "eu-north-1" }
data "aws_caller_identity" "current" {}
data "aws_ssm_parameter" "ubuntu" { name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id" }
locals {
  tags = { Project = "SecForge", ManagedBy = "Terraform", ResourceType = "application", Environment = "private-beta" }
}
resource "aws_security_group" "application" {
  name        = "secforge-v1-application"
  description = "HTTPS beta application; no public database or backend ports"
  vpc_id      = var.vpc_id
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }
  ingress {
    from_port       = 22
    to_port         = 22
    protocol        = "tcp"
    security_groups = [var.gateway_group]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "secforge-v1-application" })
}
resource "aws_vpc_security_group_ingress_rule" "gateway_management" {
  security_group_id            = var.gateway_group
  referenced_security_group_id = aws_security_group.application.id
  ip_protocol                  = "tcp"
  from_port                    = 22

  to_port     = 22
  description = "SecForge cloud backend private gateway administration"
}
resource "aws_iam_role" "application" {
  name               = "secforge-v1-application"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }] })

  tags = local.tags
}
resource "aws_iam_role_policy" "application" {
  name = "secforge-owned-lifecycle"

  role = aws_iam_role.application.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Sid = "ProtectApplication", Effect = "Deny", Action = ["ec2:TerminateInstances", "ec2:DeleteVolume", "ec2:DeleteSecurityGroup", "ec2:ModifyInstanceAttribute", "ec2:CreateTags", "ec2:DeleteTags"], Resource = "arn:aws:ec2:eu-north-1:${data.aws_caller_identity.current.account_id}:*/*", Condition = { StringEquals = { "ec2:ResourceTag/ResourceType" = "application" } } },
    { Sid = "RegionalInventory", Effect = "Allow", Action = ["ec2:Describe*", "ec2:GetConsoleOutput"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = "eu-north-1" } } },
    { Sid = "LaunchTaggedInstances", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "arn:aws:ec2:eu-north-1:${data.aws_caller_identity.current.account_id}:instance/*", Condition = { StringEquals = { "aws:RequestTag/Project" = "SecForge", "aws:RequestTag/ManagedBy" = "Terraform" } } },
    { Sid = "LaunchComponents", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = ["arn:aws:ec2:eu-north-1::image/*", "arn:aws:ec2:eu-north-1:${data.aws_caller_identity.current.account_id}:volume/*", "arn:aws:ec2:eu-north-1:${data.aws_caller_identity.current.account_id}:network-interface/*", "arn:aws:ec2:eu-north-1:${data.aws_caller_identity.current.account_id}:key-pair/${var.key_name}", "arn:aws:ec2:eu-north-1:${data.aws_caller_identity.current.account_id}:subnet/${var.challenge_subnet}", "arn:aws:ec2:eu-north-1:${data.aws_caller_identity.current.account_id}:security-group/*"] },
    { Sid = "CreateOwnedGroups", Effect = "Allow", Action = ["ec2:CreateSecurityGroup"], Resource = "arn:aws:ec2:eu-north-1:${data.aws_caller_identity.current.account_id}:security-group/*", Condition = { StringEquals = { "aws:RequestTag/Project" = "SecForge", "aws:RequestTag/ManagedBy" = "Terraform" } } },
    { Sid = "GroupVpc", Effect = "Allow", Action = ["ec2:CreateSecurityGroup"], Resource = "arn:aws:ec2:eu-north-1:${data.aws_caller_identity.current.account_id}:vpc/${var.vpc_id}" },
    { Sid = "TagOnCreation", Effect = "Allow", Action = ["ec2:CreateTags"], Resource = "arn:aws:ec2:eu-north-1:*:*/*", Condition = { StringEquals = { "ec2:CreateAction" = ["RunInstances", "CreateSecurityGroup", "CreateImage"] } } },
    { Sid = "OwnedLifecycle", Effect = "Allow", Action = ["ec2:TerminateInstances", "ec2:ModifyInstanceAttribute", "ec2:DeleteSecurityGroup", "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress", "ec2:CreateTags", "ec2:DeleteTags", "ec2:CreateImage"], Resource = ["arn:aws:ec2:eu-north-1:${data.aws_caller_identity.current.account_id}:instance/*", "arn:aws:ec2:eu-north-1:${data.aws_caller_identity.current.account_id}:security-group/*"], Condition = { StringEquals = { "ec2:ResourceTag/Project" = "SecForge", "ec2:ResourceTag/ManagedBy" = "Terraform" } } },
    { Sid = "CaptureImage", Effect = "Allow", Action = ["ec2:CreateImage"], Resource = ["arn:aws:ec2:eu-north-1::image/*", "arn:aws:ec2:eu-north-1::snapshot/*"] },
    { Sid = "OwnedCaptureRetention", Effect = "Allow", Action = ["ec2:DeregisterImage", "ec2:DeleteSnapshot"], Resource = ["arn:aws:ec2:eu-north-1::image/*", "arn:aws:ec2:eu-north-1::snapshot/*"], Condition = { StringEquals = { "ec2:ResourceTag/Project" = "SecForge", "ec2:ResourceTag/ManagedBy" = "SecForge" } } },
    { Sid = "ListState", Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation"], Resource = "arn:aws:s3:::${var.state_bucket}" },
    { Sid = "StateObjects", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = ["arn:aws:s3:::${var.state_bucket}/builders/*", "arn:aws:s3:::${var.state_bucket}/labs/*"] }
  ], [{ Effect = "Deny", Action = ["ec2:TerminateInstances", "ec2:DeleteVolume", "ec2:DeleteSnapshot", "ec2:DeregisterImage", "ec2:DeleteSecurityGroup", "ec2:CreateTags", "ec2:DeleteTags", "ec2:ModifyInstanceAttribute", "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress"], Resource = var.protected_resources }]) })
}
resource "aws_iam_instance_profile" "application" {
  name = "secforge-v1-application"
  role = aws_iam_role.application.name
}
resource "aws_instance" "application" {
  ami                         = nonsensitive(data.aws_ssm_parameter.ubuntu.value)
  instance_type               = "t3.small"
  subnet_id                   = var.subnet_id
  associate_public_ip_address = false
  vpc_security_group_ids      = [aws_security_group.application.id]
  key_name                    = var.key_name
  iam_instance_profile        = aws_iam_instance_profile.application.name
  credit_specification { cpu_credits = "standard" }
  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }
  root_block_device {
    volume_size           = 20
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = false
    tags                  = local.tags
  }

  tags = merge(local.tags, { Name = "secforge-v1-application" })
  lifecycle {
    prevent_destroy = true
    # EC2/provider reports a public association after attaching the managed EIP;
    # retain initial no-auto-IP launch policy without replacing the database host.
    ignore_changes = [associate_public_ip_address]
  }
}
resource "aws_eip" "application" {
  domain   = "vpc"
  instance = aws_instance.application.id
  tags     = merge(local.tags, { Name = "secforge-v1-application" })
}
output "instance_id" {
  value     = aws_instance.application.id
  sensitive = true
}
output "public_ip" {
  value     = aws_eip.application.public_ip
  sensitive = true
}
output "private_ip" {
  value     = aws_instance.application.private_ip
  sensitive = true
}
output "application_group" {
  value     = aws_security_group.application.id
  sensitive = true
}
