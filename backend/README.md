# Backend — ecommerce-sales-agent

FastAPI backend for the AI sales agent. This is the **foundation phase**:
configuration, database connection, cache groundwork, structured logging and
health endpoints. Agent orchestration (LangGraph), LLM gateway, Shopify and
Telegram integrations are intentionally **not implemented yet** — see
`docs/architecture-audit.md` (§P, implementation order) for the roadmap.

## Stack

| Concern | Choice |
| --- | --- |
| Framework | FastAPI + Uvicorn |
| Config | pydantic-settings (env vars + optional `.env`) |
| Database | PostgreSQL (Neon) via SQLAlchemy 2.x (sync) + psycopg3 |
| Cache | Redis (async client, groundwork only) |
| Logging | structlog (JSON in production, console in dev) |
| Tooling | pytest, ruff, mypy |

## Layout

```
backend/
├── app/
│   ├── config/          # Settings (pydantic-settings), env enum, LLM provider settings
│   ├── core/            # Cross-cutting: structured logging
│   ├── db/              # Engine/session factory tuned for Neon (no tables yet)
│   ├── integrations/    # External clients: Redis factory (Shopify/Telegram later)
│   ├── api/
│   │   ├── deps.py      # Request-scoped dependencies (DB session, Redis, settings)
│   │   └── routes/      # Routers: health (more added per phase)
│   └── main.py          # App factory + ASGI entrypoint (lifespan-managed infra)
├── tests/               # Smoke tests (pytest)
├── requirements.txt     # Runtime dependencies
├── requirements-dev.txt # Dev/CI tooling
├── pyproject.toml       # ruff + mypy configuration
└── pytest.ini
```

## Getting started

```bash
cd backend

# 1. Create a virtualenv and install dependencies
python -m venv .venv
source .venv/Scripts/activate      # Windows (Git Bash)
# source .venv/bin/activate        # Linux/macOS
pip install -r requirements-dev.txt

# 2. Configure environment (from repo root; placeholders are fine to boot)
cp ../.env.example ../.env         # then edit values

# 3. Run the API
uvicorn app.main:app --reload      # docs at http://localhost:8000/docs
```

> The app **boots without running Postgres/Redis** — connections are lazy.
> `/api/v1/health` always answers; `/api/v1/health/ready` reports dependency
> status (`degraded` when a dependency is unreachable).

## Health endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/health` | Liveness — process is up. No dependency calls. |
| `GET /api/v1/health/ready` | Readiness — probes Postgres/Neon and Redis. |

## Checks

```bash
pytest                 # smoke tests
ruff check .           # lint
ruff format --check .  # formatting
mypy app               # static types
```

## Configuration

All settings come from environment variables (see `../.env.example` for the
full annotated list; `app/config/settings.py` is the source of truth).
`DATABASE_URL` is required — the app refuses to start without it. Secrets use
`SecretStr` so they never leak into logs or `repr()`.

Database notes (Neon, per audit §L): use the **pooled** Neon connection string
in deployment; `DB_USE_NULL_POOL=true` (default) avoids holding connections
open across event loops/cold starts; `pool_pre_ping` handles serverless
resets. No application tables exist yet — Alembic migrations arrive with the
schema phase.

## Deliberately not in this phase

- LangGraph agent + tools (`app/agents/`, `app/tools/` — reserved packages)
- LLM gateway (Gemini primary / Nemotron fallback)
- Shopify / Telegram integrations
- Admin API and dashboard
- Alembic migrations / domain tables
