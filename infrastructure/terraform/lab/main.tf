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

module "lab_env" {
  source = "./modules/lab_env"

  lab_instance_id = var.lab_instance_id
  learner_id      = var.learner_id
  room_id         = var.room_id
  ami_id          = var.ami_id
  instance_type   = var.instance_type
  subnet_id       = var.subnet_id
  vpc_id          = var.vpc_id
  ssh_key_name    = var.ssh_key_name
  exposed_ports   = var.exposed_ports
  vpn_cidr        = var.vpn_cidr
  user_data       = var.user_data
  tags            = var.tags
}
