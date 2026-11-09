"""Shared FastAPI dependencies"""

import secrets

from fastapi import Header, HTTPException

from config.settings import settings


def require_admin(api_key: str = Header(None)) -> None:
    """Reject requests without the admin API key"""
    if not api_key or not secrets.compare_digest(api_key, settings.ADMIN_API_KEY):
        raise HTTPException(status_code=403, detail="Invalid API key")


def require_shopify_provisioning(
    key: str | None = Header(
        default=None,
        alias="X-Shopify-Provisioning-Key",
    ),
) -> None:
    """Authenticate requests from the Shopify app server."""
    expected = settings.SHOPIFY_PROVISIONING_API_KEY

    if len(expected) < 32:
        raise HTTPException(
            status_code=503,
            detail="Shopify provisioning is not configured",
        )

    if not key or not secrets.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(
            status_code=403,
            detail="Invalid provisioning credentials",
        )
