"""API router aggregation.

Register feature routers here as they are implemented; external integrations
(Shopify, Telegram) and agent routes are added in later phases.
"""

from fastapi import APIRouter

from app.api.routes import health

api_router = APIRouter()
api_router.include_router(health.router)
