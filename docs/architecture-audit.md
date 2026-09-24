# Architecture & Integration Audit — ecommerce-sales-agent

> **Scope note.** This is an *analysis document*, not an implementation. No code, dependencies,
> LangGraph setup, Shopify/Telegram/admin-dashboard implementations, or DB migrations are included.
> Nothing here has been committed.
>
> **Verification status.** The Inventra repository was **not accessible** from this workspace
> (the only reference found in this repo is a one-line mention in `README.md`). Everything stated
> about Inventra is therefore explicitly labeled **[Assumption]** and must be validated against
> the actual Inventra codebase before implementation. Sections whose statements are grounded in
> the local repo or in verified external research are marked accordingly. This document
> deliberately does **not** invent existing Inventra functionality.

---

## A. System Responsibilities

| Component | Owns | Does NOT own |
| --- | --- | --- |
| **Telegram integration** | Receiving customer messages (webhook), sending replies via Bot API, normalizing payloads into internal events | Any business logic, product logic, LLM calls, DB schema |
| **Sales Agent (orchestration)** | Driving the conversation turn: call LLM, decide tools, execute tools, produce a reply | Direct DB access to Inventra, direct Shopify calls scattered in LLM code |
| **LangGraph** | The graph of agent states (intent → retrieve → compose → act → respond), state schema, checkpoints, retries per node | Business rules themselves; those live in tools/services |
| **LLM layer (Gemini primary, Nemotron fallback)** | NL understanding & generation only | Deciding business policy — that is graph/prompt/tool design |
| **Agent tools** | Typed wrappers over backend services: product search, suggestions, details, stock checks | Inventing data; they delegate to the Inventra client |
| **Inventra integration (client)** | All product/inventory reads, service-to-service auth, mapping Inventra DTOs → agent-facing schemas | Writing to Inventra data; the agent is read-mostly for v1 |
| **Shopify integration** | Order/checkout/catalog boundary with the external e-commerce platform | Replacing Inventra; the two are complementary external systems |
| **Inventra (external, existing)** | Product/inventory source of truth: search, availability, stock levels | Being rebuilt or accessed at the DB level from this project |
| **PostgreSQL (Neon)** | System-of-record for *our* domain: conversations, messages, customers, sessions, agent state | Product/inventory truth (that stays in Inventra) |
| **Redis** | Transient/ephemeral state only (see §G) | Any data that must survive restarts — that belongs in Postgres |
| **Admin dashboard (frontend)** | Human-facing UI: conversation list, WhatsApp-like thread view, message history | Talking directly to Inventra/Shopify/Telegram APIs |
| **Admin backend API** | Serving conversation/session data to the dashboard, admin auth, RBAC | Exposing Inventra internals |

Key architecture idea (confirmed as sound): the Sales Agent **never touches Inventra's database**.
The boundary is:

```
Customer → Telegram → Sales Agent → LangGraph → Agent Tools → Inventra API → Inventra services/DB
```

---

## B. Proposed Integration Boundaries

```
                         ┌─────────────────────────────┐
   Customer              │  ecommerce-sales-agent      │
      │                  │                             │
      ▼                  │  ┌───────────────────────┐  │      ┌──────────────┐
  Telegram               │  │ Telegram Adapter      │◄─┼────► │  Telegram    │
  (webhook/Bot API) ────►│  └──────────┬────────────┘  │      │  Cloud       │
                         │             │ (internal     │      └──────────────┘
                         │             │  events)      │
                         │             ▼               │
                         │  ┌───────────────────────┐  │      ┌──────────────┐
                         │  │ LangGraph Agent       │──┼────► │ Gemini (primary)
                         │  │ (nodes: classify →    │  │      │ Nemotron (fallback)
                         │  │  retrieve → suggest → │  │      └──────────────┘
                         │  │  compose → respond)   │  │
                         │  └────┬─────────────┬────┘  │      ┌──────────────┐
                         │       │             │       ├────► │ Inventra API │──► Inventra
                         │       ▼             ▼       │      │ (HTTP, m2m)  │    services/DB
                         │  ┌──────────┐ ┌──────────┐  │      └──────────────┘
                         │  │ Admin    │ │ Shopify  │  │      ┌──────────────┐
                         │  │ Backend  │ │ Adapter  │◄─┼────► │ Shopify Admin │
                         │  └────▲─────┘ └──────────┘  │      │ API / webhooks│
                         │       │                     │      └──────────────┘
                         │  ┌────┴─────────────────┐   │      ┌──────────────┐
                         │  │ Postgres (Neon)      │   │      │ Redis        │
                         │  │ + Redis              │   │      └──────────────┘
                         │  └──────────────────────┘   │      ┌──────────────┐
                         └─────────────────────────────┘      │ Admin UI     │──► Admins
                                                              └──────────────┘
```

Boundary rules:

1. **Telegram ⇄ backend:** one direction of ingress (webhook) + Bot API for egress. Only our
   backend holds the bot token. Telegram never calls anything but our webhook endpoint.
2. **Agent tools ⇄ Inventra:** HTTP(S) only, service-to-service authenticated (see §M), read-only
   product/inventory operations in v1. **No shared DB, no customer JWTs.**
3. **Agent tools ⇄ Shopify:** HTTP(S) only, separate adapter, server-side tokens; customer identity
   is *referenced* (Shopify customer id), never proxied.
4. **Admin UI ⇄ admin backend:** our own REST API + admin session auth. Admin UI never bypasses
   our backend to reach Inventra, Shopify, Telegram, or the DB.
5. **LangGraph ⇄ everything:** LangGraph sees only typed tools and typed state; it has no URL,
   token, or schema knowledge of external systems.

---

## C. Inventra Reuse Opportunities

**Verified:** Inventra is described as the *existing product/inventory system* to be integrated,
not rebuilt. It is **not accessible** from this workspace, so no file-level verification of its
API surface was possible.

**[Assumption]** Inventra is an existing system with product/inventory data and some existing API
surface (any e-commerce inventory system used by other consumers almost certainly exposes at least
product listing/detail and stock endpoints).

Reusable with high confidence (pending verification):

- **Product/inventory data model** — do not duplicate product truth in our DB; reference Inventra
  product IDs in our domain where needed.
- **Existing product/inventory read endpoints** — if present, they become the substrate for the
  agent tools; no rebuild needed.
- **Operational posture** — Inventra presumably has its own deployment, DB, migrations, auth.
  Reuse means *calling it*, not embedding it.

Non-reusable / must live in this project:

- Conversation persistence, admin chat UI, LangGraph orchestration, Telegram transport, LLM access,
  caching layers — all of this is ecommerce-sales-agent's own domain.

---

## D. Required Inventra Changes

For the first capability (**product suggestion tool**), the agent needs these read-only operations
from Inventra. Each is listed as **Needs to exist (assumed)** vs. **reuse if present** — this must
be reconciled with a real inspection of Inventra's API:

| # | Operation | Suggested shape | Status |
| --- | --- | --- | --- |
| 1 | Product search/query | `GET /api/v1/products?q=&category=&price_min=&price_max=&limit=&cursor=` | Reuse if present; else add |
| 2 | Product suggestions | `GET /api/v1/products/suggest?q=&limit=` — lightweight, typo-tolerant, returns a small ranked set | Likely **new** — suggestion/recommendation semantics usually don't exist in CRUD APIs |
| 3 | Product details | `GET /api/v1/products/{id}` | Reuse if present |
| 4 | Availability/stock check | `GET /api/v1/products/{id}/availability?variant=&qty=` returning `in_stock / out_of_stock / low_stock` + qty | Likely **new** — internal inventory systems rarely expose agent-friendly availability semantics |
| 5 | Bulk availability for shortlists | `POST /api/v1/products/availability:batch { ids: [...] }` — one call to check N candidates (agent shortlists several, then presents top-k) | Likely **new** |
| 6 | Category/facet listing | `GET /api/v1/categories` (for narrowing search) | Reuse if present |

Cross-cutting requirements for the agent-facing Inventra API surface:

- **Read-only scope.** v1 agent tools are read-only; no mutations cross this boundary in v1.
- **Pagination** (cursor-based) and **bounded page sizes** so the agent can't pull whole tables.
- **Stable IDs + `updated_at`** on every resource (feeds cache invalidation and incremental sync later).
- **Rate-limit headers** so our client can back off correctly.
- **Deterministic JSON schemas** (OpenAPI) so tool signatures and validation can be generated/checked.
- **Optional but recommended:** a machine-readable *capabilities* endpoint (`/api/v1/health` or
  `/api/v1/capabilities`) for circuit-breaker health checks.

> ⚠️ The "likely new" entries are assumptions until Inventra's actual API is inspected. The audit
> deliberately does not assert which endpoints already exist.

---

## E. Agent/Tool Requirements

The first capability is a **product suggestion tool**. Minimum tool set:

1. `search_products(query, filters?, limit)` → wraps Inventra search.
2. `suggest_products(intent_summary, context?, limit)` → wraps the suggestion endpoint; returns
   ranked candidates with id, name, price, short reason.
3. `get_product_details(product_id)` → wraps product details.
4. `check_availability(product_id, variant?, qty?)` → wraps availability endpoint.

Tool design rules:

- **Typed, small I/O contracts** — tools return validated dataclasses/Pydantic models, not raw JSON.
- **One direction of dependency:** tools → `InventraClient` → HTTP → Inventra API. Tools never
  import Inventra internals.
- **Idempotent, cache-friendly** — tools are pure reads, safe to cache (§G).
- **Failure semantics:** tools return structured errors (e.g., `ToolUnavailable`, `ToolTimeout`,
  `EmptyResult`) so the graph can degrade gracefully ("I can't reach the catalog right now") rather
  than crash the turn.
- **No LLM in tools.** Tools never call the LLM; only graph nodes do.
- **Timeouts on every outbound call** (Inventra, LLMs) with explicit budgets (e.g., Inventra 3s,
  LLM 20s) so a turn has a bounded worst-case latency.

---

## F. Gemini + Nemotron Strategy

**Verified externally:** NVIDIA hosts Nemotron behind an **OpenAI-compatible** endpoint
(`https://integrate.api.nvidia.com/v1`) with `nvapi-...` keys, consumable via the OpenAI SDK or
LangChain's `ChatOpenAI`/`ChatNVIDIA`. Gemini is consumed via Google GenAI SDK or `langchain-google-genai`.

Recommended pattern — **one internal `LlmGateway` abstraction**:

- Interface: `complete(messages, tools_schema) → LlmResponse`.
- Primary: **Gemini** (via `langchain-google-genai` or REST).
- Fallback: **Nemotron** (via OpenAI-compatible client pointed at NVIDIA's endpoint).
- **Failover trigger:** after N retries with backoff on 429/5xx/timeout from Gemini, or on
  hard quota exhaustion, swap to Nemotron for that turn (sticky for the conversation turn, not
  permanently).
- **Health-based routing:** a small circuit breaker per provider (closed → open → half-open), state
  in Redis (§G) so all replicas share breaker state.
- **Config alignment with Inventra [Assumption]:** the same conventions as any Spring/Node/Python
  service would use — provider, model name, temperature, max_tokens, timeout, retry policy in one
  config object; secrets via env (`GEMINI_API_KEY`, `FALLBACK_LLM_API_KEY`,
  `FALLBACK_LLM_BASE_URL`, `FALLBACK_LLM_MODEL` — all already present in `.env.example`).
- **Normalization caveat:** tool/function-calling formats differ between providers. Keep the
  gateway's contract provider-neutral (JSON tool schema in/out) and adapt per provider at the edge,
  so a swap to Nemotron doesn't change tool code.
- **Model pins:** pin exact model identifiers in config (e.g., `models/gemini-2.5-flash` and the
  specific Nemotron checkpoint), never "latest".

---

## G. Caching Strategy

Redis is required — but not everything belongs in Redis. Allocation:

| Data | Where | Why / TTL guidance |
| --- | --- | --- |
| Inventra product/inventory reads (search results, product details, availability) | **Redis**, short TTL (30–120s) + negative caching for "not found" (5–10s) | Cross-process sharing, protects Inventra from tool-call storms; inventory is volatile → short TTL; use `updated_at`/ETag for early invalidation if Inventra exposes it |
| LLM circuit-breaker state, provider health | **Redis** | Must be shared across replicas |
| Per-chat rate limiting (Telegram + LLM budgeting) | **Redis** (e.g., token bucket per chat/user) | Cross-process correctness requires shared store |
| In-flight LLM response assembly, per-turn scratch state | **In-process memory** | Ephemeral, single-turn lifetime, no cross-request value |
| Admin dashboard conversation list (first page, per admin) | **In-process memory, seconds** | Cheap, avoids Redis round-trip for UI polling; DB is source of truth |
| LangGraph conversation state | **Postgres** (`PostgresSaver`), *not* Redis | Must survive restarts, supports long-lived threads; Redis could be a hot cache *in front of* Postgres later if needed |
| Conversation/message rows | **Postgres** | System of record |
| Static catalog-ish data (categories, facets) | **Redis**, longer TTL (hours) + invalidation on Inventra change | Changes rarely |

Anti-patterns to avoid:

- Caching LLM responses keyed by prompt (wrong on multi-tenant, multi-model, tool-schema drift).
- Caching conversation history in Redis as system of record.
- Caching customer PII with long TTLs or without encryption-at-rest posture.
- Duplicating Inventra's inventory truth into our own cache as a second source of truth.

---

## H. Telegram Architecture

```
Customer → Telegram → HTTPS webhook → our backend → internal event → LangGraph turn → reply via Bot API
```

Design points:

- **Webhook (not polling)** in production; long polling acceptable in local dev.
- **HMAC verification** of Telegram webhook requests using `TELEGRAM_WEBHOOK_SECRET` (already in
  `X-Telegram-Bot-Api-Secret-Token` header) — reject anything else at the edge.
- **Idempotency:** Telegram retries webhooks on non-2xx; key idempotency on Telegram's
  `update_id` to avoid duplicate turns.
- **Ordering & concurrency:** per-chat serialization (one turn at a time per chat; queue or lock in
  Redis) to avoid interleaved replies from concurrent updates.
- **Ack-fast, process-async pattern:** return 200 quickly and process the turn asynchronously with a
  bounded worker pool, so Telegram doesn't time out and retry during a slow LLM turn.
- **Reply transport:** sendMessage (and later, typing indicators). Message length/formatting limits
  handled in the Telegram adapter, not the agent.
- **Identity mapping:** persist `telegram_user_id`, `username`, names → our `customer` row; the
  agent sees an internal customer id, never raw transport details.
- No admin functionality flows through Telegram; admins use the dashboard.

---

## I. Admin Dashboard / Conversation Architecture

Requirement: admins see customer conversations in a **WhatsApp-like interface** —
conversations listed, opening one shows chronological message history, customer vs. agent messages
distinguishable.

Backend must expose (REST, admin-session-authenticated):

- `GET /admin/conversations?status=&cursor=` — list with last-message preview + timestamp.
- `GET /admin/conversations/{id}/messages?before=&limit=` — paginated chronological history.
- (v2 options: `POST .../messages` for admin takeover, read/unread counters, websocket push.)

**Minimum entity model** (design only — no migrations yet):

```
customers
- id (uuid, pk)
- telegram_user_id (bigint, unique)      -- external identity
- username, first_name, last_name
- locale, created_at, updated_at

conversations
- id (uuid, pk)
- customer_id → customers.id
- channel = 'telegram'
- status (open | closed | needs_human)   -- for routing/handoff later
- last_message_at, created_at, updated_at
  (list view sorted by last_message_at desc)

messages
- id (uuid, pk)
- conversation_id → conversations.id (indexed with (conversation_id, created_at))
- role ('customer' | 'agent' | 'system' | 'admin')   -- distinguishes customer vs agent msgs
- content_text
- content_metadata (jsonb: tool calls made, product cards shown, LLM model used, fallback flag)
- correlation_id (groups one agent turn: customer msg + tool calls + agent reply)
- created_at
```

Why this satisfies the requirements: conversations list (query on `conversations` ordered by
`last_message_at`), chronological history (composite index on `(conversation_id, created_at)`),
customer-vs-agent distinction (`messages.role`), history durability (Postgres, not Redis).

Relationship to LangGraph state:

- **Postgres is the conversation system of record** (what the dashboard reads).
- **LangGraph checkpoints (`PostgresSaver`) are the agent's execution state** (thread id =
  conversation id), used for resumption/consistency — *not* the dashboard's data source.
- Keep the two separate; derive the dashboard view from `conversations`/`messages`, never from
  checkpoint blobs.

---

## J. Shopify Integration Boundary

Everything crossing the Shopify boundary is external-system data; nothing from Shopify becomes
internal truth except ids we deliberately persist as references.

Inbound to our system (high level):

- Order/fulfillment events via **webhooks** (HMAC-verified, `SHOPIFY_WEBHOOK_SECRET`), e.g.
  order-paid, order-refunded → enrich conversation context ("your order shipped").
- Customer/order references that let the agent answer "where is my order" questions later.

Outbound from our system:

- Product *enrichment* reads (Shopify catalog as an alternative surface to Inventra data) —
  reconcile carefully: **Inventra = inventory truth, Shopify = commerce/checkout truth.**
- Checkout/payment links generation, order status queries (server-side Admin API with
  `SHOPIFY_ACCESS_TOKEN`).
- Never proxy customer credentials; agent acts on behalf of the store, referencing Shopify
  customer/order ids.

Explicitly out of scope for v1: Shopify storefront customizations, theme changes, cart
manipulation. Not implemented in this phase.

---

## K. LangGraph Responsibility

LangGraph owns **orchestration**, not intelligence and not data access:

- **Graph of nodes:** e.g., `classify_intent → gather_context (tools) → suggest (tools + LLM) →
  compose_reply → persist/emit`. Conditional edges for escalation (`needs_human`) and errors.
- **State schema:** typed state (customer id, conversation id, message history window, tool results,
  escalation flag).
- **Checkpoints:** `PostgresSaver` (thread = conversation id) — durable, resumable, production-grade
  (verified: official `langgraph-checkpoint-postgres` package).
- **Retries per node** with bounded attempts; tool failures become state, not exceptions that kill
  the turn.
- **Time-travel/debuggability** as an operational bonus (inspect a turn's path from checkpoints).

Explicitly NOT LangGraph's job: prompt contents (that's LLM layer), product data (Inventra),
message transport (Telegram adapter), UI (admin dashboard), cache policy (Redis layer).

LLM vs. LangGraph vs. tools:

- **LLM** decides *what to say* and *which tool to request* given schemas.
- **LangGraph** decides *what happens next* (routing, retries, fallbacks, persistence).
- **Tools** do *what was decided* (typed calls to Inventra/Shopify clients).

---

## L. Database Requirements (design only)

PostgreSQL on **Neon**. Tables needed for the conversation architecture (§I) plus operational tables:

```
customers        (see §I)
conversations    (see §I)
messages         (see §I)

agent_runs (optional but recommended for observability)
- id, conversation_id, started_at, finished_at, status
- model_used, fallback_used (bool), tool_calls (jsonb), token_usage, latency_ms, error
```

Notes:

- All ids UUIDs; timestamps `timestamptz`; jsonb for flexible metadata.
- Key indexes: `conversations(last_message_at desc)`, `messages(conversation_id, created_at)`,
  unique `customers(telegram_user_id)`.
- LangGraph checkpoint tables are created by `langgraph-checkpoint-postgres` itself (do not hand-roll
  them); run them in the same Neon DB, separate schema if desired.
- **Migrations:** use a real migration tool (Alembic recommended) — but do **not** create them now.
- Neon specifics: serverless Postgres → use a pooled connection string (Neon pooler), short-lived
  connections, and be mindful of cold starts for admin list queries.

---

## M. Security / Service Authentication

**Rejected approach:** passing the *customer's* JWT (or any customer credential) from agent →
Inventra as the service-to-service credential. The customer authenticated with *us* (via Telegram,
which has its own identity); the agent's reads from Inventra are on behalf of the *store*, not the
customer. Customer JWTs also expire, may not exist for Telegram-only customers, and would grant
Inventra the ability to impersonate customers — wrong trust direction.

**Recommended production-ready approach — machine-to-machine (m2m) auth:**

1. **Preferred: OAuth 2.0 Client Credentials.** If Inventra exposes (or can expose) an OAuth2
   client-credentials token endpoint: this project registers as a confidential client
   (`INVENTRA_CLIENT_ID`/`INVENTRA_CLIENT_SECRET`), obtains short-lived access tokens (5–60 min),
   caches them in Redis (keyed to expire before token expiry, with refresh margin), and sends
   `Authorization: Bearer <token>`. Inventra validates the token and can enforce scopes
   (`products:read`, `inventory:read`). Supports rotation, audit, revocation.
2. **Acceptable if Inventra cannot support OAuth2:** a dedicated **service API key** issued by
   Inventra *specifically for the sales agent*, sent in a custom header (e.g., `X-Service-Token`),
   scoped server-side to read-only product/inventory endpoints, with IP allowlisting where possible.
   Rotate via dual-key overlap.
3. **Defense in depth (either option):**
   - Restrict to server-to-server network paths (private networking / IP allowlist).
   - Separate credentials per environment (dev/staging/prod), all via env/secrets manager — never
     in the repo (`.env.example` already carries placeholders).
   - **Authorization** on our side: admin endpoints require admin sessions (RBAC roles, e.g.,
     `admin`/`viewer`); agent tools have no admin authority; the Telegram webhook is HMAC-verified.
   - Audit logging of service calls (who/what/when) for production accountability.

---

## N. Production-Readiness Considerations

| Concern | Recommendation |
| --- | --- |
| **Authentication** | m2m OAuth2 client credentials (or scoped service key) for Inventra; Telegram webhook HMAC; Shopify webhook HMAC; admin sessions for dashboard |
| **Authorization** | RBAC on admin API (`admin` vs `viewer`); tool scope-limiting at Inventra (`products:read`, `inventory:read`); no customer credentials crossing service boundaries |
| **Rate limiting** | Redis token buckets: per Telegram chat (protect LLM spend), per admin-API client, and respect Inventra's limits via client-side throttling + `Retry-After` |
| **Error handling** | Typed errors at each boundary; graph-level degradation ("catalog unavailable" replies); global exception handlers; no stack traces to customers |
| **Logging** | Structured JSON logs with `conversation_id` + `correlation_id` on every line; request IDs propagated through Telegram → graph → tools |
| **Retries** | Exponential backoff + jitter on Inventra/LLM calls; retry only idempotent calls; bounded attempts (2–3); then LLM fallback / graceful message |
| **Caching** | As §G — Redis for shared/short-TTL data, in-process for per-request UI reads, Postgres for durable state |
| **Secrets** | Env vars/secrets manager; `.env` gitignored, `.env.example` placeholders only; separate creds per environment; rotation plan for bot token, LLM keys, Inventra/Shopify tokens |
| **Testing** | Unit tests for tools/clients (mocked HTTP), contract tests for Inventra adapter (recorded fixtures), graph tests with fake LLM, E2E happy-path (mock Telegram), load test the webhook path |
| **Deployment** | Containerized backend; CI with typecheck+tests; separate dev/staging/prod envs; Neon branching for preview envs; health/readiness endpoints; IaC later |
| **Observability** | Metrics: turn latency, LLM fallback rate, tool error rate, cache hit rate; alert on fallback-rate spikes and Inventra 5xx |
| **Data protection** | PII minimization in logs; retention policy for messages; encryption in transit everywhere; DB backups (Neon PITR) |

---

## O. Open Decisions / Questions

1. **Inventra API surface (blocking):** What endpoints actually exist today for search/details/
   availability? Is there an OpenAPI spec? *Must be answered by inspecting Inventra — the single
   biggest unknown in this audit.*
2. **Inventra auth capability (blocking):** Does Inventra support OAuth2 client credentials, or
   only API keys? Determines §M option 1 vs 2.
3. **Inventra network reachability:** Can our backend reach Inventra over private networking, or
   public internet only (→ stricter allowlisting/TLS posture)?
4. **Nemotron specifics:** Which Nemotron model/checkpoint exactly (e.g., a Nemotron Ultra vs.
   Super variant), and is NVIDIA's hosted NIM endpoint the intended path, or self-hosted NIM?
5. **Human handoff:** Should admins be able to *take over* a conversation from the dashboard (send
   as admin/human)? Changes the message model (already reserved `admin` role) and adds a write API
   + Telegram-side sending on behalf of a human.
6. **Conversation retention:** How long are conversations retained? PII/compliance constraints?
7. **Languages:** Which locales must the agent support (affects prompts, caching of localized
   content, and message metadata)?
8. **Rate-limit budget:** What is the acceptable cost/latency envelope per turn (LLM token budget,
   Inventra call budget) to size rate limiters and timeouts?
9. **Admin auth source:** Internal username/password + RBAC, SSO/OIDC later?
10. **Multi-tenancy:** Single store, or multiple Shopify/Inventra tenants from one deployment?

---

## P. Recommended Implementation Order

1. **Resolve blockers in §O.1–2** — inspect Inventra's actual API + auth options (read-only
   inspection; no modifications).
2. **Backend skeleton:** FastAPI app factory, config/settings, structured logging, health endpoints,
   CI (typecheck + tests) — no business logic.
3. **Inventra client + contract tests:** typed client, m2m auth (per §M), recorded fixtures; build
   the read-only endpoints Inventory needed in §D if they don't exist yet (Inventra-side work).
4. **Database foundation:** Alembic setup + `customers`/`conversations`/`messages` (schema from §I),
   using a pooled Neon connection.
5. **Telegram transport:** webhook endpoint with HMAC verification + idempotency + ack-fast/async
   processing; persist customer + conversation + raw customer message. (No agent yet — echo/ack only.)
6. **LangGraph agent v0:** trivial graph (echo → classify → canned reply) with `PostgresSaver`;
   prove checkpointing + per-chat serialization.
7. **LLM gateway:** Gemini primary + Nemotron fallback with circuit breaker (state in Redis),
   pinned models, provider-neutral tool schema.
8. **Product suggestion capability:** implement §E tools against the Inventra client; wire the
   `suggest` node; golden-set evals for suggestion quality.
9. **Admin backend API:** conversations list + message history endpoints with admin auth + RBAC.
10. **Admin dashboard frontend:** WhatsApp-like UI on top of the admin API.
11. **Shopify integration:** webhooks (HMAC) + order-status reads, after core agent value works.
12. **Production hardening:** rate limiting, observability dashboards/alerts, load tests, security
    review, runbooks, staging → prod rollout.

---

### Verification Summary

| Claim class | Status |
| --- | --- |
| Local repo state (scaffold, commit `6698fb5`, clean tree) | **Verified** |
| Inventra functionality/endpoints | **Not verified — not accessible** (all marked [Assumption]) |
| Nemotron OpenAI-compatible endpoint (`integrate.api.nvidia.com/v1`) | **Verified via web research** |
| LangGraph Postgres/Redis checkpointers exist | **Verified via web research** |
| Tool/graph/schema designs | **Proposals** — pending Inventra reconciliation |
