"""FastAPI application for CartPE Sync"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routes import (
    cron_router,
    stores_router,
    subscriptions_router,
)
from config.settings import validate_settings
from utils.logger import setup_logger

setup_logger("api")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_settings()
    yield


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
