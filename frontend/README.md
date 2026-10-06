# Admin Dashboard (frontend)

Minimal React + Vite + TypeScript dashboard for the Sales Agent's admin APIs:
customer conversations plus integrated read-only Inventra views (products,
inventory, stock movements, categories, overview stats).

## Layout

- **Left pane**: conversation list (customer name, last-message preview,
  status badge, most recent activity first, paginated).
- **Right pane**: chronological message thread of the selected conversation
  (customer bubbles left, agent bubbles right).
- **Inventra pages**: Overview (dashboard stats), Products, Inventory,
  Stock Movements, Categories — served by the backend's admin Inventra API
  (no direct Inventra connection from the browser; no Inventra source changes).

## Run (development)

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

The Vite dev server proxies `/api/*` to `http://127.0.0.1:8000` (FastAPI),
so no CORS setup is needed locally.

## Backend configuration

The backend must have the admin-auth environment variables set (see
`.env.example` in the repo root):

| Variable | Purpose |
| --- | --- |
| `ADMIN_USERNAME` | login username |
| `ADMIN_PASSWORD_HASH` | PBKDF2 hash — generate with `python -m app.api.admin_deps` (from `backend/`) |
| `JWT_SECRET_KEY` | secret used to sign admin JWTs |
| `ADMIN_JWT_EXPIRE_MINUTES` | token lifetime (default 480) |
| `CORS_ALLOWED_ORIGINS` | optional; not needed with the dev proxy |

## Auth flow

1. `POST /api/v1/admin/login` with username/password → `{access_token, token_type}`.
2. Token is kept in `sessionStorage` (cleared on logout / tab close).
3. Every admin request sends `Authorization: Bearer <token>`; a 401 surfaces
   the error in the UI.
