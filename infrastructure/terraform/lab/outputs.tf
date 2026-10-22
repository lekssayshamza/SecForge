output "instance_id" {
  description = "EC2 instance ID"
  value       = module.lab_env.instance_id
}

output "private_ip" {
  description = "Private IP address of the learner lab instance"
  value       = module.lab_env.private_ip
}

output "security_group_id" {
  description = "Security group ID"
  value       = module.lab_env.security_group_id
}
