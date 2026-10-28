"""FastAPI application for CartPE Sync"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routes import (
    cron_router,
    stores_router,
    subscriptions_router,
)
from config.settings import validate_settings
from services.shopify_delivery_queue import retry_pending_deliveries
from utils.logger import setup_logger

setup_logger("api")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_settings()

    async def retry_shopify():
        while True:
            try:
                await asyncio.to_thread(retry_pending_deliveries)
            except Exception as error:
                logger.error("Shopify retry maintenance deferred: %s", type(error).__name__)
            await asyncio.sleep(5)

    retries = asyncio.create_task(retry_shopify())
    try:
        yield
    finally:
        retries.cancel()
        try:
            await retries
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="CartPE Sync API",
    description="API for managing stores, subscriptions, and product sync",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(stores_router)
app.include_router(subscriptions_router)
app.include_router(cron_router)


@app.get("/")
def read_root() -> dict:
    return {
        "message": "CartPE Sync API",
        "version": "1.0.0",
        "endpoints": {
            "stores": "/api/stores",
            "register_subscription": "/api/subscriptions/register",
            "add_permissions": "/api/subscriptions/permissions",
            "store_credentials": "/api/subscriptions/credentials",
            "subscription_status": "/api/subscriptions/status",
            "manual_push": "/api/subscriptions/push/{subscription_id}",
            "extend_subscription": "/api/subscriptions/extend/{subscription_id}",
            "cron_status": "/api/cron/status",
            "cron_update": "/api/cron/update",
            "cron_toggle": "/api/cron/toggle",
        },
    }
