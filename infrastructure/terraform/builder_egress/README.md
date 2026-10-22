# Private creator-builder internet route

This isolated module owns exactly one private-subnet IPv4 default route and
TCP 80/443 ingress from that subnet to the existing gateway security group.
It does not create or replace a gateway, instance, subnet or managed NAT Gateway.

The security-group rule is not the authorization boundary. Before applying,
install the fail-closed gateway policy and enable the backend's exact-IP creator
builder synchronization. Learner addresses must never appear in that policy.
See [the deployment overview](../../../docs/DEPLOYMENT.md) and the operator's private gateway runbook.

Use a private, separate Terraform working directory/state and an explicitly
verified `targets.tfvars.json` containing `route_table_id`,
`gateway_network_interface_id`, `gateway_group_id` and `private_cidr`. Keep
identifiers and state out of shared reports. Review a saved plan: only the three
resources in this module may change. Never apply shared VPC/environment modules
for this repair. Preserve the existing VPN return route and route associations.

```sh
terraform init -input=false
terraform validate
terraform plan -input=false -var-file=targets.tfvars.json -out=approved.tfplan
# Inspect the exact saved plan privately before this explicitly authorized step:
terraform apply -input=false approved.tfplan
```

Rollback must stop the new authorization synchronizer from recreating rules
before removing its uniquely marked forward rule and owned NAT/guard table.
Remove only this module's default route and exact created ingress rule IDs.
Restore the backed-up on-disk gateway config without flushing/reloading the live
VPN firewall. Do not destroy any environment, shared networking or AMI.
