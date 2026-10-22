# Deployment and recovery overview

This is a sanitized description of the existing deployment, not authorization to
deploy or a replacement for the owner's private release and recovery records.
No production addresses, account lists, private keys, credentials or resource
inventories are included here. Repository preparation is entirely local.

## Existing topology

- One application EC2 host runs Caddy, PostgreSQL and the systemd `secforge`
  service. FastAPI listens on loopback; Caddy serves the frontend and proxies
  `/api` while preserving the API prefix.
- One Uvicorn process owns the existing reconciliation and expiration tasks.
  Additional workers or replicas are not a supported deployment shortcut because
  lifecycle operation locks are process-local.
- Builders and learner machines are separate private EC2 environments. The
  existing VPN gateway and authorization rules control access. Per-room TCP/UDP
  settings restrict challenge ingress to configured VPN sources.
- The backend's configured source directories select the builder/lab Terraform
  modules. The application module is a separate deployment concern. Existing
  source files include protected-resource safeguards that must be preserved.

Source templates live in `backend/deploy/cloud/`, privileged wrappers in
`backend/deploy/`, and infrastructure modules in `infrastructure/terraform/`.
The application `terraform.tfvars.example` intentionally contains unusable
placeholders. Never apply it or legacy per-room modules without a separately
reviewed environment, plan and authorization.

## Configuration and keys

The established production service uses a root-only environment file outside the
checkout. It carries its own database URL and stable application secrets. SSH
private keys, VPN client profiles, OAuth/DNS credentials, Terraform state/variables,
database backups and uploaded private media are not GitHub source files.

JWT signing, answer verification and recoverable answer encryption have distinct
roles. Preserve the verification key and the AES-256 editable-answer key with the
matching database backups. The latter also protects accepted learner submissions.
Do not substitute development keys, regenerate production keys during deployment,
or publish keys through Vite variables. Key changes require an explicit recovery
and rotation decision.

## Established release sequence

1. Review a scoped source/static diff against the running release. Preserve
   unrelated work and confirm a compatible rollback. Do not apply Terraform just
   because its source directory exists.
2. Verify the target database and make a complete private recovery set using the
   existing backup workflow. Check checksums, retain an off-host copy and rehearse
   restoration to a disposable database. Backups include database data, private
   media and configuration; same-disk copies alone are not disaster recovery.
3. Rehearse required additive migrations using
   `backend/deploy/cloud/migrate.py`. It is not a database initialization command.
   Never run `app.init_db`, the development seed, or a database restore over an
   existing production database as part of a normal code release.
4. Use the existing release process to preserve previous files, coordinate only
   the necessary API restart, and keep one lifecycle worker. Defer the cutover
   when conflicting provisioning, capture or cleanup operations are active.
5. Build the frontend with `VITE_API_BASE_URL=''`. Retain old hashed assets and
   replace the frontend entry only after backend/database health has recovered.
6. Verify the intended read-only application behavior, deployed file hashes,
   permissions and logs. Production acceptance does not require changing real
   answers, credit, publication state or running lab resources.

These steps describe review requirements; exact host selections, commands,
privileged access, backup manifests and rollback artifacts stay with the operator.
Historical evidence/runbooks under `backend/deploy/cloud/*.md` and selected dated
reports elsewhere remain on disk but are excluded from the initial Git commit.

## Rollback

Prefer the compatible previous application/static release using its recorded
rollback procedure. Keep additive database columns, newer learner data, stable
keys, uploaded media, state and AMI references. Do not automatically restore an old
dump over newer user activity. A code rollback does not authorize terminating
instances, deleting snapshots, changing VPN rules or replacing shared resources.

If a release unexpectedly requires a destructive migration, production key change,
resource replacement or unrelated infrastructure operation, stop for the owner's
explicit approval before performing it.
