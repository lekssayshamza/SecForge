output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.builder.id
}

output "private_ip" {
  description = "Private IP address of the builder instance"
  value       = aws_instance.builder.private_ip
}

output "security_group_id" {
  description = "Security group ID"
  value       = aws_security_group.builder.id
}