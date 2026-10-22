# Local development

- Linux is the validated development platform.
- Python 3.11+ with virtual-environment support. Current local validation uses
  Python 3.14; dependencies are pinned in `backend/requirements.txt`.
- Node.js 22 and npm; current local validation uses Node 22.22.2. Use the committed
  `frontend/package-lock.json` with `npm ci`.
- PostgreSQL 18 for production-compatible database testing. SQLite requires no
  separate database service for the offline quick start.
- Git for source control. AWS credentials, SSH keys, Terraform and a VPN are
  **not required** for the offline quick start.

The disposable PostgreSQL harness expects PostgreSQL 18 binaries under
`/usr/lib/postgresql/18/bin`. Live provisioning additionally requires the existing
approved AWS/VPN deployment and Terraform 1.10+; enabling it is not part of setup.

## Offline local development

Use a fresh local database and locally generated keys. Do not copy production
environment files, answer keys, user databases or VPN profiles into a checkout.
The example disables real provisioning, VPN synchronization and background workers.
Machine/image lifecycle responses in this mode are simulations, not usable EC2 labs.

From the repository root:

```sh
cd backend
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

# Refuse to overwrite an existing environment. If one exists, review it separately.
test ! -e .env && (umask 077; cp .env.example .env)
```

For a newly copied example only, replace its three key placeholders without
printing any generated values:

```sh
python - <<'PY'
import base64
import os
from pathlib import Path
import secrets

path = Path('.env')
text = path.read_text()
replacements = {
    '<GENERATE_LOCAL_JWT_SECRET>': secrets.token_urlsafe(48),
    '<GENERATE_LOCAL_VERIFICATION_KEY>': secrets.token_urlsafe(48),
    '<GENERATE_LOCAL_BASE64URL_AES256_KEY>': base64.urlsafe_b64encode(os.urandom(32)).decode(),
}
if not all(text.count(marker) == 1 for marker in replacements):
    raise SystemExit('Expected an unchanged example; existing keys were not modified.')
for marker, value in replacements.items():
    text = text.replace(marker, value)
path.write_text(text)
path.chmod(0o600)
print('Local key placeholders replaced; values were not printed.')
PY

mkdir -p .local/avatars
export AWS_EC2_METADATA_DISABLED=true

# Only for this fresh LOCAL database; never initialize an existing production DB.
python -B -m app.init_db
PYTHONPATH=. python -B deploy/cloud/migrate.py
python -B -m app.seed_dev

uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

`app.seed_dev` creates a known development-only creator and prints its local login
details. It refuses production mode. Do not run it against an existing shared or
production database. There is no Alembic migration chain: the maintained additive
migration entry point is `backend/deploy/cloud/migrate.py`.

Keep `CHALLENGE_ANSWER_KEY` stable for persisted grading data. `EDITABLE_ANSWER_KEY`
must encode exactly 32 bytes and protects both recoverable author answers and
learners' accepted submissions. Keep the corresponding keys with private backups;
JWT rotation is independent. Never replace these keys to make an existing database
start, commit them, or place them in frontend configuration.

In a second terminal, from the repository root:

```sh
cd frontend
npm ci
test ! -e .env.local && (umask 077; cp .env.example .env.local)
npm run dev
```

Open `http://localhost:3000`. An empty `VITE_API_BASE_URL` uses same-origin `/api`;
Vite proxies it to the local API on port 8000. Backend health is at
`http://127.0.0.1:8000/health`; development API documentation is at `/docs`.
Production API documentation is disabled.

For local PostgreSQL instead of SQLite, prepare a separate local database and
set its connection URL in your ignored `.env` before initialization. The optional
`docker-compose.yml` provides only a local PostgreSQL service with development
credentials; it is not the production architecture or a production credential source.

The examples override operator-specific path defaults using paths relative to
`backend/`. Run backend commands from that directory. Live AWS settings remain
unusable placeholders until an independently reviewed environment is configured.
