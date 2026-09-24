# ecommerce-sales-agent

## Project Objective

**ecommerce-sales-agent** is an AI-powered Sales Agent for an e-commerce system.
It will interact with customers on Telegram, understand their requests using an
LLM-driven agent, and act on real store data (catalog, inventory, orders) to
help customers discover products and complete purchases.

> **Status: Implementation has not started yet.**
> This repository currently contains only the planned project structure
> (directories, `.gitignore`, `.env.example`, and this `README.md`).
> No application logic, frameworks, integrations, or dependencies are in place.

## High-Level Planned Components

| Component | Purpose |
| --- | --- |
| **Telegram** | Customer-facing messaging channel; the entry point for conversations with the sales agent. |
| **Sales Agent** | Core conversational agent that interprets customer intent and orchestrates responses and actions. |
| **LangGraph** | Agent orchestration framework used to model the sales workflow as a stateful graph of steps. |
| **LLM + Fallback** | Primary LLM (Gemini) for natural-language understanding/generation, with a fallback LLM provider for resilience. |
| **Agent Tools** | Reusable tools the agent can invoke (product lookup, cart/order operations, inventory checks, etc.). |
| **Inventra** | Inventory system integration for stock and product availability data. |
| **Shopify** | E-commerce platform integration for store data, products, and orders. |
| **PostgreSQL / Neon** | Primary relational database (Neon serverless Postgres) for persistent application data. |
| **Caching** | Cache layer (Redis) for performance on hot paths such as product/inventory lookups. |

## Planned Repository Layout

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

Not applicable yet — no dependencies are installed and no code exists.
See `.env.example` for the configuration variables the project expects to use.
