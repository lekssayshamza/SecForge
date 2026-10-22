output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.lab.id
}

output "private_ip" {
  description = "Private IP address of the learner lab instance"
  value       = aws_instance.lab.private_ip
}

output "security_group_id" {
  description = "Security group ID"
  value       = aws_security_group.lab.id
}
