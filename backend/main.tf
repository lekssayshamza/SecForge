terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

provider "aws" {
  region = "eu-north-1"
}

module "builder_env" {
  source = "./modules/builder_env"

  builder_id    = var.builder_id
  creator_id    = var.creator_id
  room_id       = var.room_id
  ami_id        = var.ami_id
  instance_type = var.instance_type
  subnet_id     = var.subnet_id
  vpc_id        = var.vpc_id
  ssh_key_name  = var.ssh_key_name
  exposed_ports = var.exposed_ports
  vpn_cidr      = var.vpn_cidr
  tags          = var.tags
}