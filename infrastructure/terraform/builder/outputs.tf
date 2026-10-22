output "instance_id" {
  description = "EC2 instance ID"
  value       = module.builder_env.instance_id
}

output "private_ip" {
  description = "Private IP address of the builder instance"
  value       = module.builder_env.private_ip
}

output "security_group_id" {
  description = "Security group ID"
  value       = module.builder_env.security_group_id
}