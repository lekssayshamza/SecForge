resource "aws_instance" "lab" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.lab.id]
  key_name                    = var.ssh_key_name
  user_data                   = var.user_data != "" ? var.user_data : file("${path.module}/bootstrap.sh")
  associate_public_ip_address = false

  tags = merge(var.tags, local.ownership_tags, { Name = "lab-${var.lab_instance_id}" })
}
