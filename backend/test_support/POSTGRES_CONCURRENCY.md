# Disposable PostgreSQL lifecycle tests

The seventeen lifecycle cases have passed against disposable PostgreSQL.
Use `--include-challenges` to add eight learning/award/migration cases; all twenty-five have passed.
Ordinary pytest collection skips it before importing the application. The only
supported activation path is the separate runner, executed as a non-root user:

```sh
cd /home/hamza/secforge/backend
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B run_postgres_concurrency.py --allow-disposable-postgres --port 55439
```

The port must be explicitly selected and cannot be 5432. There is no URL option
or application DATABASE_URL fallback. The runner creates a private, mode-0700
temporary directory under /tmp and invokes installed PostgreSQL 18 initdb and
pg_ctl with bounded timeouts. PostgreSQL listens only on that directory's Unix
socket, with TCP disabled. Local trust authentication is restricted by the
private directory and socket permissions; no password is generated or logged.
It creates a dedicated non-superuser role and disposable database, revoking
public access to the other connectable databases in its own cluster.

The runner clears inherited environment configuration, changes into its empty
owned work directory before settings import (so no project .env is read), and
supplies its generated database URL and synthetic settings. Each case creates
the actual application model metadata in a fresh schema, including PostgreSQL
partial indexes. API get_db, background-task and reconciliation SessionLocal
bindings use separate real PostgreSQL connections. No application init_db or
application-wide initialization is invoked. The optional learning cases explicitly exercise their additive migration inside their private fixture schema. Existing SQLite fixtures are unchanged.

Python network operations, boto3/botocore clients, process spawning and shell
commands are blocked during tests; psycopg accepts only the validated private
socket/database/role. Terraform/workspace preparation and VPN authorization are
faked. Selector waits are bounded to 10ms because restricted wakeup sockets can
otherwise hang asyncio thread completion in the offline execution sandbox. This
changes loop scheduling, not PostgreSQL transactions or lock enforcement; test
ordering still requires events or observed database blockers. No SQL/transaction
substitution, failure filtering or shared-connection fixture is used.
Pytest plugin autoload, cache and bytecode
are disabled. Only the explicitly selected PostgreSQL modules run; pytest's exit status is kept.

Tests reuse existing event-controlled provisioning interleavings. Concurrent
Stop pauses after the first real SELECT FOR UPDATE, observes the second actor
in pg_blocking_pids, and then releases the first. Concurrent creation meets at
a barrier before either INSERT, allowing PostgreSQL's real partial unique index
to choose one winner. Timeouts detect hangs; no elapsed delay establishes order.

Shutdown verifies data-directory/socket/process ownership and uses bounded fast
shutdown. Deletion requires the original inode/UID/private permissions and an
unchanged ownership marker. If startup, shutdown or ownership is uncertain,
cleanup fails closed and retains the temporary artifacts; it never kills an
unverified process or touches development/Compose storage. A future run must
check its exit status and confirm cleanup. Same-OS-user malicious tampering and
machine-level failures are outside this test harness's isolation guarantee.

Offline harness checks (no database or control binaries execute):

```sh
cd /home/hamza/secforge/backend
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -B -m pytest -p no:cacheprovider test_postgres_harness_safety.py -q
```

Learning concurrency and migration cases (same isolation safeguards):

```sh
cd /home/hamza/secforge/backend
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B run_postgres_concurrency.py --allow-disposable-postgres --port 55439 --include-challenges
```
