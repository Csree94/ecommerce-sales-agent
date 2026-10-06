# ecommerce-sales-agent

## Project Objective

**ecommerce-sales-agent** is an AI-powered Sales Agent for an e-commerce system.
It will interact with customers on Telegram, understand their requests using an
LLM-driven agent, and act on real store data (catalog, inventory, orders) to
help customers discover products and complete purchases.

> **Status: Project 1 COMPLETE (October 2026).** Implemented: Telegram sales agent
> (FastAPI + LangGraph + Gemini/Nemotron LLMs), confirmed-purchase flow with inventory
> deduction via Inventra, Redis read-through cache with stock-out invalidation,
> JWT-protected admin dashboard (conversations + Inventra views), PostgreSQL/Neon
> persistence. Shopify is intentionally **not** part of Project 1 (it is Project 2).
> See `backend/README.md` and `docs/architecture-audit.md` for details.

## High-Level Planned Components

| Component | Purpose |
| --- | --- |
| **Telegram** | Customer-facing messaging channel; the entry point for conversations with the sales agent. |
| **Sales Agent** | Core conversational agent that interprets customer intent and orchestrates responses and actions. |
| **LangGraph** | Agent orchestration framework used to model the sales workflow as a stateful graph of steps. |
| **LLM + Fallback** | Primary LLM (Gemini) for natural-language understanding/generation, with a fallback LLM provider for resilience. |
| **Agent Tools** | Reusable tools the agent can invoke (product lookup, cart/order operations, inventory checks, etc.). |
| **Inventra** | Inventory system integration for stock and product availability data. |
| **Shopify** | E-commerce platform integration — **Project 2 scope, not implemented in Project 1**. |
| **PostgreSQL / Neon** | Primary relational database (Neon serverless Postgres) for persistent application data. |
| **Caching** | Cache layer (Redis) for performance on hot paths such as product/inventory lookups. |

## Repository Layout (as built)

```
ecommerce-sales-agent/
├── backend/     # API + agent services (FastAPI, LangGraph, tools, integrations)
├── frontend/    # Web frontend (React + Vite)
├── tests/       # Test suites
├── docs/        # Project documentation
├── .env.example # Environment variable template (placeholders only)
├── .gitignore
└── README.md
```

## Getting Started

See `backend/README.md` (API + agent), `frontend/README.md` (admin dashboard), and
`.env.example` (all configuration variables; secrets stay in a gitignored `.env`).
Security expectations: admin password stored only as a PBKDF2-SHA256 hash, JWT-protected
admin APIs, deny-by-default CORS, Telegram webhook secret verification, all secrets via
env vars (`SecretStr`) — never committed.

Testing evidence at completion: 377/377 backend tests (incl. 18 cache, 52 admin),
18/18 live webhook end-to-end checks, 33/33 real-Redis cache/invalidation checks.
