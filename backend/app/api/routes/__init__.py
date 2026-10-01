"""API router aggregation.

Register feature routers here as they are implemented. Customer channels
(Telegram webhook) and the JWT-protected admin dashboard APIs live side by
side; external integrations (Shopify) follow in later phases.
"""

from fastapi import APIRouter

from app.api.routes import admin, health, inventra_admin, telegram

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(telegram.router)
api_router.include_router(admin.router)
api_router.include_router(inventra_admin.router)
