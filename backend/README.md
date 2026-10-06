# Backend — ecommerce-sales-agent

FastAPI backend for the AI sales agent. **Project 1 scope is fully implemented**:
configuration, database persistence, structured logging, health endpoints, the
Telegram webhook + LangGraph agent (classify → gather → purchase → compose →
persist) with Gemini primary / Nemotron fallback LLMs, the Inventra integration
(reads + confirmed-purchase stock-out), a Redis read-through cache with
stock-out invalidation, and the JWT-protected admin API backing the dashboard.
Shopify remains **Project 2** (config placeholders only) — see
`docs/architecture-audit.md` for the architecture and this file for layout/runbook.

## Stack

| Concern | Choice |
| --- | --- |
| Framework | FastAPI + Uvicorn |
| Config | pydantic-settings (env vars + optional `.env`) |
| Database | PostgreSQL (Neon) via SQLAlchemy 2.x (sync) + psycopg3 |
| Cache | Redis (read-through cache for Inventra reads; degrades safely when absent) |
| Logging | structlog (JSON in production, console in dev) |
| Tooling | pytest, ruff, mypy |

## Layout

```
backend/
├── app/
│   ├── config/          # Settings (pydantic-settings), env enum, LLM provider settings
│   ├── core/            # Cross-cutting: structured logging
│   ├── db/              # Engine/session factory tuned for Neon
│   ├── models/          # SQLAlchemy 2.x models: Customer, Conversation, Message, AgentRun
│   ├── migrations/      # Alembic environment + versioned migrations
│   ├── integrations/    # External clients: Inventra (reads + stock-out), Redis + cache, LLM gateway, Telegram
│   ├── agents/          # LangGraph sales agent (classify/gather/purchase/compose/persist)
│   ├── tools/           # Typed Inventra tool wrappers (cache-aside aware)
│   ├── api/
│   │   ├── deps.py      # Request-scoped dependencies (DB session, Redis, settings)
│   │   └── routes/      # Routers: health, telegram webhook, admin, admin Inventra views
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
pytest                 # full suite (377 tests at Project 1 completion)
ruff check .           # lint (4 documented, intentionally-deferred findings in scripts/)
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
resets.

## Database schema & migrations

Models live in `app/models/` (application data only — Inventra stays the
source of truth for products/inventory; LangGraph checkpoints are owned by
`langgraph-checkpoint-postgres` and are never part of this metadata):

| Table | Purpose |
| --- | --- |
| `customers` | Customer identity, unique `telegram_user_id` |
| `conversations` | Chat thread per customer; `last_message_at` drives dashboard ordering |
| `messages` | Turn history; roles `customer/agent/system/admin`; Telegram update-id idempotency |
| `agent_runs` | Per-turn observability (model, fallback, tool calls, latency) |

```bash
# Apply migrations (uses DATABASE_URL from the environment)
alembic upgrade head
# Generate SQL without a DB (offline check)
alembic upgrade head --sql
# Create a new revision after model changes (needs a reachable DB)
alembic revision --autogenerate -m "change"
```

## Deliberately not in this phase

- Shopify integration (Project 2; config placeholders only — no code, no calls)
- LangGraph Postgres checkpoints (owned by `langgraph-checkpoint-postgres`)
