"""API router aggregation.

Register feature routers here as they are implemented. The Telegram webhook
is the first customer-facing channel route; external integrations (Shopify)
and admin routes follow in later phases.
"""

from fastapi import APIRouter

from app.api.routes import health, telegram

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(telegram.router)
