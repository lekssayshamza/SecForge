# Local tests

Run tests with synthetic keys and disposable data, never a production environment
or a running application database. Backend test modules provide their own mocks;
forcing every configuration object into dry-run mode changes several tests'
assumptions. The recipe below instead blocks unmocked infrastructure commands
and external network connections, and disables the imported application's worker.

## Backend regression suite

After installing `backend/requirements.txt`, activate that virtual environment
and run this from `backend/`. The empty temporary working directory prevents the
real `.env` from being loaded. The database and fixtures are removed afterwards.
Use an ordinary non-root development account.

```sh
python -B - <<'PY'
import base64
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile

backend = Path.cwd().resolve()
assert (backend / 'app/main.py').is_file(), 'Run from backend/'
sys.path.insert(0, str(backend))
sys.dont_write_bytecode = True

with tempfile.TemporaryDirectory(prefix='secforge-tests-') as folder:
    root = Path(folder)
    os.environ.clear()
    os.environ.update(
        PATH='/usr/bin:/bin', LANG='C.UTF-8', HOME=str(root / 'home'),
        DATABASE_URL='sqlite:///' + str(root / 'bootstrap.sqlite'),
        JWT_SECRET=secrets.token_urlsafe(48),
        EDITABLE_ANSWER_KEY=base64.urlsafe_b64encode(os.urandom(32)).decode(),
        TERRAFORM_DRY_RUN='false', AWS_EC2_METADATA_DISABLED='true',
        VPN_ENABLED='false', VPN_AUTHZ_ENABLED='false',
        VPN_BACKEND_AUTHZ_ENABLED='false',
        AVATAR_STORAGE_DIR=str(root / 'avatars'),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1',
    )
    os.chdir(root)
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    def local(address):
        return not isinstance(address, tuple) or address[0] in ('127.0.0.1', 'localhost', '::1')
    def guarded_connect(sock, address):
        if not local(address):
            raise RuntimeError('External network disabled in local tests')
        return connect(sock, address)
    def guarded_connect_ex(sock, address):
        if not local(address):
            raise RuntimeError('External network disabled in local tests')
        return connect_ex(sock, address)
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    popen = subprocess.Popen
    def offline_popen(args, *values, **kwargs):
        command = args[0] if isinstance(args, (list, tuple)) and args else args
        if Path(str(command)).name in {
            'aws', 'terraform', 'ssh', 'scp', 'sftp', 'ssh-keyscan', 'curl',
            'wget', 'sudo', 'fsfreeze', 'systemctl', 'reboot', 'shutdown',
        }:
            raise RuntimeError('Live infrastructure commands disabled in local tests')
        return popen(args, *values, **kwargs)
    subprocess.Popen = offline_popen
    from app.config import settings
    settings.reconciliation_enabled = False
    from app.database import Base, engine
    import app.models
    Base.metadata.create_all(engine)
    import pytest
    try:
        status = pytest.main([
            str(backend), '-q', '-p', 'no:cacheprovider', '--tb=short',
            '--basetemp', str(root / 'pytest'), '--confcutdir', str(backend),
        ])
    finally:
        engine.dispose()
        os.chdir(backend)
raise SystemExit(status)
PY
```

This uses normal test configuration defaults with mocks; it does not enable live
Terraform operations. PostgreSQL-only tests skip in this run. It is not proof of
AWS provisioning or VPN connectivity, and does not launch or capture a machine.

## Disposable PostgreSQL integration suite

The existing harness creates a separately owned PostgreSQL 18 cluster, listens
only on its private Unix socket, clears inherited credentials, guards external
operations and removes only its own test resources. It does not accept a
production DSN as an opt-in. Binaries must exist at `/usr/lib/postgresql/18/bin`.
Choose an unused, non-default unprivileged port; this example port is not a server
connection target or a shared database setting.

From `backend/` with its virtual environment active:

```sh
python -B run_postgres_concurrency.py \
  --allow-disposable-postgres --port 55447 \
  --include-challenges --include-profiles --include-lab-time --include-auth \
  --include-workspace --include-social --include-room-media \
  --include-network-ports --include-editable-answers --include-learner-answers
```

The suite covers migrations, concurrent grading/credit, publication/image
selection, profiles, authentication, timing and the encrypted-answer lifecycle.

## Frontend

From `frontend/`:

```sh
npm ci
npm test
npm run typecheck
npm run lint
VITE_API_BASE_URL='' npm run build
```

Frontend tests use synthetic APIs. Optional `*-browser.py` acceptance scripts in
`frontend/tests/` use local fixtures and Chromium; inspect their documented
requirements before running them. Production browser tests are not part of this
local setup. Browser scripts, logs, screenshots and real authentication tokens
from operator acceptance runs must remain outside the committed source tree.

Existing deprecation warnings and the Vite large-chunk warning do not themselves
mean a test failed. Preserve test exit status and investigate unexpected failures;
do not weaken access checks, omit assertions or silently filter failing tests.
