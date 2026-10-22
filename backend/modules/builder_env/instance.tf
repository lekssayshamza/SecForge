resource "aws_instance" "builder" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.builder.id]
  key_name                    = var.ssh_key_name
  user_data                   = file("${path.module}/bootstrap.sh")
  associate_public_ip_address = false

  tags = merge(var.tags, { Name = "builder-${var.builder_id}" })
}