"""Webhook routes"""

import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from config.settings import settings
from services.push_orchestrator import PushOrchestrator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["webhooks"])


class SummaryPayload(BaseModel):
    created: int = 0
    stock_updated: int = 0
    stock_unchanged: int = 0
    errors: list = []
    skip: list[dict] = []


class WebhookPayload(BaseModel):
    status: str
    summary: SummaryPayload
    token: str
    buyer_domain: str
    timestamp: str = ""


@router.post("/webhooks/push-complete")
async def push_complete_webhook(
    payload: WebhookPayload, x_webhook_secret: str = Header(None)
) -> dict:
    """Record a completed push reported by a buyer's site"""
    if not x_webhook_secret or x_webhook_secret != settings.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    PushOrchestrator.update_last_push_at(
        token=payload.token, buyer_domain=payload.buyer_domain
    )
    logger.info(
        f"Push reported for {payload.buyer_domain} — created: {payload.summary.created}, "
        f"stock_updated: {payload.summary.stock_updated}, "
        f"skipped: {len(payload.summary.skip)}"
    )
    if payload.summary.errors:
        logger.warning(
            f"Push reported errors for {payload.buyer_domain}: "
            f"{payload.summary.errors}"
        )

    return {"success": True}
