"""Subscription routes"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from mysql.connector import Error as MySQLError
from pydantic import BaseModel, field_validator

from api.deps import require_admin
from config.database import DatabaseManager
from services.csv_service import CSVService
from services.subscription_service import SubscriptionService
from utils.timeutil import parse_date

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["subscriptions"])


class SubscriptionCreateRequest(BaseModel):
    buyer_name: str
    buyer_domain: str
    phone_number: str

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        cleaned = "".join(filter(str.isdigit, v))
        if len(cleaned) != 10:
            raise ValueError("Phone number must be exactly 10 digits")
        return f"+91{cleaned}"


class PermissionAddRequest(BaseModel):
    token: str
    buyer_domain: str
    store_ids: list[int]


class CredentialsRequest(BaseModel):
    token: str
    buyer_domain: str
    consumer_key: str
    consumer_secret: str


class SubscriptionStatusRequest(BaseModel):
    token: str
    buyer_domain: str


class ExtendSubscriptionRequest(BaseModel):
    expires_at: str


@router.post("/subscriptions/register", dependencies=[Depends(require_admin)])
async def register_subscription(request: SubscriptionCreateRequest) -> dict:
    """Register a new subscriber"""
    try:
        subscription = SubscriptionService.create_subscription(
            buyer_name=request.buyer_name,
            buyer_domain=request.buyer_domain,
            phone_number=request.phone_number,
        )
        return {"success": True, "data": subscription}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except MySQLError as e:
        logger.error(f"Error registering subscription: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@router.post("/subscriptions/permissions")
async def add_permissions(request: PermissionAddRequest) -> dict:
    """Set which stores a subscription can access"""
    try:
        result = SubscriptionService.add_subscription_permissions(
            token=request.token,
            buyer_domain=request.buyer_domain,
            store_ids=request.store_ids,
        )
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except MySQLError as e:
        logger.error(f"Error adding permissions: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@router.post("/subscriptions/credentials")
async def store_credentials(request: CredentialsRequest) -> dict:
    """Store the buyer's WordPress REST credentials"""
    try:
        result = SubscriptionService.store_woocommerce_credentials(
            token=request.token,
            buyer_domain=request.buyer_domain,
            consumer_key=request.consumer_key,
            consumer_secret=request.consumer_secret,
        )
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except MySQLError as e:
        logger.error(f"Error storing credentials: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@router.post("/subscriptions/status")
async def get_subscription_status(request: SubscriptionStatusRequest) -> dict:
    """Get a subscription's expiry and selected stores"""
    try:
        result = SubscriptionService.get_subscription_status(
            token=request.token, buyer_domain=request.buyer_domain
        )
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except MySQLError as e:
        logger.error(f"Error getting status: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@router.post(
    "/subscriptions/push/{subscription_id}",
    dependencies=[Depends(require_admin)],
)
async def manual_push(subscription_id: int) -> dict:
    """Generate and upload a CSV for one subscriber."""
    try:
        csv_path = CSVService.generate_csv_for_subscription(subscription_id)
    except (MySQLError, OSError, ValueError):
        logger.exception(f"CSV generation failed for subscription {subscription_id}")
        raise HTTPException(
            status_code=500,
            detail="CSV generation failed",
        ) from None

    if not csv_path:
        raise HTTPException(
            status_code=404,
            detail="No active subscription or no eligible stores to export",
        )

    if not CSVService.upload_csv(csv_path, subscription_id):
        raise HTTPException(
            status_code=500,
            detail="CSV upload failed",
        )

    return {
        "success": True,
        "message": f"CSV uploaded for subscription {subscription_id}",
    }


@router.post(
    "/subscriptions/extend/{subscription_id}", dependencies=[Depends(require_admin)]
)
async def extend_subscription(
    subscription_id: int, request: ExtendSubscriptionRequest
) -> dict:
    """Set a subscription's expiry date"""
    try:
        expires_at = parse_date(request.expires_at)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
        )

    with DatabaseManager.get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT id FROM api_subscriptions WHERE id = %s FOR UPDATE",
                (subscription_id,),
            )
            if cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail="Subscription not found")

            cursor.execute(
                "UPDATE api_subscriptions SET expires_at = %s WHERE id = %s",
                (expires_at, subscription_id),
            )
        finally:
            cursor.close()

    return {"success": True, "expires_at": request.expires_at}
