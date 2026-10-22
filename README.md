# SecForge

SecForge is a full-stack cybersecurity training platform for hands-on labs and
structured learning. Creators build and publish challenge rooms; learners explore
content, connect to isolated lab machines through a VPN, and track their progress.

## Key features

- **Room authoring:** Markdown briefings, tasks, questions, hints, and publication workflows.
- **Practical labs:** Ubuntu EC2 builders, challenge image capture, and temporary learner environments.
- **Controlled networking:** VPN access and room-specific TCP/UDP port configuration.
- **Learning and progress:** Server-side answer verification, persistent points, completion tracking, and leaderboards.
- **Content discovery:** Search, category filters, bookmarks, and learning paths.
- **Account security:** Role-based authorization and encrypted storage for recoverable answers.
- **Responsive interface:** Shared room cards, creator workspace tabs, and light/dark themes.

## Technology stack

| Layer | Technologies |
| --- | --- |
| Frontend | React, TypeScript, Vite, React Router, TanStack Query |
| Backend | Python, FastAPI, SQLAlchemy, Pydantic |
| Database | PostgreSQL; SQLite for offline development |
| Infrastructure | AWS EC2, Terraform, VPN networking |
| Application hosting | Caddy, systemd |

## Getting started

Use Linux, Python 3.11+, and Node.js 22. Local development can run with SQLite
and simulated infrastructure, without AWS credentials or a VPN.

```sh
git clone https://github.com/lekssayshamza/SecForge.git
cd SecForge/backend
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Follow [Local development](docs/DEVELOPMENT.md) to copy the sanitized environment
examples, generate local keys, initialize a fresh development database, and start
the API. The example configuration disables live cloud operations.

Then, in a second terminal:

```sh
cd SecForge/frontend
npm ci
npm run dev
```

Open **http://localhost:3000**. The frontend proxies API requests to the local
backend on port 8000. Environment examples are provided for the backend, frontend,
and application Terraform module; real environment files and keys stay outside Git.

## Tests and build

```sh
cd frontend
npm test
npm run typecheck
npm run lint
VITE_API_BASE_URL='' npm run build
```

[Testing](docs/TESTING.md) includes the isolated backend suite and disposable
PostgreSQL integration tests. [Deployment overview](docs/DEPLOYMENT.md) describes
the application topology and release considerations.

## Project structure

```text
frontend/                   React application and frontend tests
backend/app/                API, domain models, and lifecycle services
backend/test_support/       Isolated testing utilities
backend/deploy/             Deployment scripts and service templates
infrastructure/terraform/   Application, builder, and learner lab modules
docs/                       Development, testing, and deployment guides
```

Lab environments are intended for authorized cybersecurity training only.
