# Builder and learner cleanup recovery

Stop is idempotent: an already stopping or stopped environment returns its
current state without scheduling another destroy. Status and Stop select the
active builder for a room, or its newest historical builder when none is active.
Existing creator/learner ownership checks still apply.

Each cleanup attempt uses the existing per-environment operation lock and
Terraform workspace/backend key. It always runs `init -reconfigure` before
destroy, including when a failed initialization left a directory behind.
Retained configuration is preserved; missing workspaces are reconstructed.
There is no AWS termination fallback or broad Terraform operation.

In real mode, cleanup requires read-only EC2 permissions for describing
instances, security groups, and network interfaces. It verifies the complete
environment ownership tags, recorded resource identities, and security-group
attachments. Only the current modules' instance and security group are accepted
as managed state resources; unexpected/shared resources block destruction.
Missing or stale state with existing owned AWS resources requires operator
review. Explicitly absent state is acceptable only when AWS absence is also
verified, including for a Stop before provisioning started.

Success requires both empty managed Terraform state and no remaining owned EC2
instance or security group. Terminating/stopped instances still count as
remaining. Identifiers and local workspace are cleared only after the
conditional `stopping` to `stopped` database completion succeeds. If the commit
fails, a retry can verify absence without repeating an unnecessary destroy.
Dry-run mode retains its simulated behavior and makes no external calls.

Failures stay `stopping`, retaining identifiers, state location and workspace.
The API `error_message` starts with `Cleanup retry required:` and names a safe
failure stage/category. Raw Terraform/provider output is not persisted there.
Reconciliation retries after the existing stopping grace period (Terraform
timeout plus reconciliation interval). Each command has the configured timeout;
EC2 reads have bounded timeouts, pagination and one SDK attempt. There are no
immediate retry loops. Known legacy cleanup failures in `failed` are also
eligible; unrelated failed provisioning is not automatically destroyed.

Automatic recovery requires a running reconciliation worker and working database,
state backend and AWS permissions. Repeated verification failures remain visible
and require operator attention; never relabel an environment stopped to hide
them. Do not delete retained state or manually terminate an unclassified
resource. The supported operation lock is process-local; multiple backend workers
remain outside the deployment assumptions.

The offline regression tests are `test_cleanup_recovery.py`, the existing lab
lifecycle tests, and the opt-in PostgreSQL concurrency suite. Actual Terraform
destroy/EC2 disappearance still needs a controlled live test using a confirmed
disposable challenge with no creator work. Published AMIs, the VPN gateway,
shared networking and unclassified resources are never cleanup targets.
